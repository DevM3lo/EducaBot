from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
import os
import re
import json
import hashlib
import secrets
import sqlite3
import logging
import time
import requests
from tenacity import retry, stop_after_attempt, wait_fixed

from groq import Groq
from google import genai
from google.genai import types

from langchain_groq import ChatGroq
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_community.document_loaders import DirectoryLoader, TextLoader, PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from router import interceptar_mensagem, analisar_matematica
from prompts import PROMPT_MATEMATICA, PROMPT_PORTUGUES

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
PASTA_DOCS = "docs"
DB_SQLITE = os.getenv("DB_PATH", "historico_chat.db")
DB_DIR = os.path.dirname(DB_SQLITE) or "."
FAISS_CACHE_DIR = os.path.join(DB_DIR, "faiss_index")
FAISS_HASH_FILE = os.path.join(FAISS_CACHE_DIR, "docs_hash.txt")

app = FastAPI(title="Educa Bot - Multimodal Cloud", version="4.0")

vector_store = None
llm_mat = None
llm_port = None
groq_client = None
gemini_client = None


def _sanitizar_erro(erro: Exception) -> str:
    msg = f"{type(erro).__name__}"
    resp = getattr(erro, "response", None)
    if resp is not None and hasattr(resp, "status_code"):
        msg += f" (HTTP {resp.status_code})"
    return msg


# ==========================================
# BANCO DE DADOS (SQLITE NO VOLUME /data)
# ==========================================
def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS conversas
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT,
                  role TEXT,
                  content TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    c.execute('''CREATE TABLE IF NOT EXISTS quizzes
                 (quiz_id TEXT PRIMARY KEY,
                  chat_id TEXT,
                  materia TEXT,
                  topico TEXT DEFAULT 'Geral',
                  pergunta TEXT,
                  opcoes_json TEXT,
                  correta TEXT,
                  explicacao TEXT,
                  respondido INTEGER DEFAULT 0)''')

    # Migração automática caso a tabela quizzes já exista sem a coluna topico
    c.execute("PRAGMA table_info(quizzes)")
    colunas_quizzes = [col[1] for col in c.fetchall()]
    if "topico" not in colunas_quizzes:
        c.execute("ALTER TABLE quizzes ADD COLUMN topico TEXT DEFAULT 'Geral'")

    c.execute('''CREATE TABLE IF NOT EXISTS placar
                 (chat_id TEXT PRIMARY KEY,
                  acertos INTEGER DEFAULT 0,
                  total INTEGER DEFAULT 0)''')

    c.execute('''CREATE TABLE IF NOT EXISTS desempenho_topicos
                 (chat_id TEXT,
                  materia TEXT,
                  topico TEXT,
                  acertos INTEGER DEFAULT 0,
                  erros INTEGER DEFAULT 0,
                  PRIMARY KEY (chat_id, materia, topico))''')

    c.execute('''CREATE TABLE IF NOT EXISTS estado_chat
                 (chat_id TEXT PRIMARY KEY,
                  materia_atual TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS updates_processados
                 (update_id INTEGER PRIMARY KEY,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.commit()
    conn.close()


def registrar_update_id(update_id: int) -> bool:
    """Retorna True se o update for inédito, False se for reenvio duplicado do Telegram."""
    if update_id is None:
        return True
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO updates_processados (update_id) VALUES (?)", (int(update_id),))
    inedito = (c.rowcount == 1)
    if inedito:
        c.execute(
            "DELETE FROM updates_processados WHERE update_id NOT IN "
            "(SELECT update_id FROM updates_processados ORDER BY update_id DESC LIMIT 2000)"
        )
    conn.commit()
    conn.close()
    return inedito


def salvar_mensagem(chat_id: str, role: str, content: str):
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute("INSERT INTO conversas (chat_id, role, content) VALUES (?, ?, ?)", (str(chat_id), role, content))
    conn.commit()
    conn.close()


def limpar_historico(chat_id: str):
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute("DELETE FROM conversas WHERE chat_id = ?", (str(chat_id),))
    c.execute("DELETE FROM estado_chat WHERE chat_id = ?", (str(chat_id),))
    conn.commit()
    conn.close()


def obter_historico_recente(chat_id: str, limite: int = 10) -> list[tuple[str, str]]:
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM (SELECT id, role, content FROM conversas WHERE chat_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        (str(chat_id), limite)
    )
    rows = c.fetchall()
    conn.close()
    return rows


def obter_materia_salva(chat_id: str) -> str | None:
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute("SELECT materia_atual FROM estado_chat WHERE chat_id = ?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def salvar_materia_atual(chat_id: str, materia: str):
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO estado_chat (chat_id, materia_atual) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET materia_atual = excluded.materia_atual",
        (str(chat_id), materia)
    )
    conn.commit()
    conn.close()


def obter_topicos_fracos(chat_id: str) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute(
        "SELECT materia, topico, erros FROM desempenho_topicos "
        "WHERE chat_id = ? AND erros > 0 AND erros >= acertos "
        "ORDER BY erros DESC LIMIT 3",
        (str(chat_id),)
    )
    rows = c.fetchall()
    conn.close()
    return rows


init_db()


# ==========================================
# INICIALIZAÇÃO DOS MOTORES DE IA E RAG
# ==========================================
def _calcular_hash_docs(pasta: str) -> str:
    if not os.path.exists(pasta):
        return ""
    hasher = hashlib.sha256()
    arquivos = []
    for root, _, files in os.walk(pasta):
        for nome in files:
            if nome.lower().endswith((".txt", ".pdf")):
                caminho = os.path.join(root, nome)
                rel = os.path.relpath(caminho, pasta)
                stat = os.stat(caminho)
                arquivos.append((rel, stat.st_mtime_ns, stat.st_size))
    if not arquivos:
        return ""
    for rel, mtime, size in sorted(arquivos):
        hasher.update(f"{rel}|{mtime}|{size}\n".encode("utf-8"))
    return hasher.hexdigest()


@app.on_event("startup")
def startup_event():
    global vector_store, llm_mat, llm_port, groq_client, gemini_client
    logger.info("Inicializando infraestrutura multimodal...")

    modelo_groq = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    try:
        try:
            llm_mat = ChatGroq(model=modelo_groq, temperature=0.1, max_tokens=3000, reasoning_effort="medium")
            llm_port = ChatGroq(model=modelo_groq, temperature=0.1, max_tokens=3000, reasoning_effort="low")
        except TypeError:
            llm_mat = ChatGroq(model=modelo_groq, temperature=0.1, max_tokens=3000)
            llm_port = llm_mat
        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        logger.info(f"Groq conectado ({modelo_groq} + Whisper Audio).")
    except Exception as e:
        logger.error(f"Erro ao conectar Groq: {_sanitizar_erro(e)}")

    try:
        gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
        logger.info("Google Gemini conectado (Embeddings + Visão 2.5 Flash).")
    except Exception as e:
        logger.error(f"Erro ao conectar Google Gemini: {_sanitizar_erro(e)}")
        embeddings = None

    if embeddings:
        hash_atual = _calcular_hash_docs(PASTA_DOCS)
        hash_salvo = ""
        if os.path.exists(FAISS_HASH_FILE):
            try:
                with open(FAISS_HASH_FILE, "r", encoding="utf-8") as f:
                    hash_salvo = f.read().strip()
            except Exception:
                pass

        if hash_atual and hash_atual == hash_salvo and os.path.exists(FAISS_CACHE_DIR):
            try:
                vector_store = FAISS.load_local(FAISS_CACHE_DIR, embeddings, allow_dangerous_deserialization=True)
                logger.info("Índice FAISS carregado do cache (docs sem alterações).")
                return
            except Exception as e:
                logger.warning(f"Falha ao ler cache FAISS, reindexando: {_sanitizar_erro(e)}")

        if hash_atual and os.path.exists(PASTA_DOCS):
            logger.info("Indexando materiais da pasta docs/...")
            docs = []
            try:
                text_splitter = RecursiveCharacterTextSplitter(chunk_size=600, chunk_overlap=100)
                loader_txt = DirectoryLoader(PASTA_DOCS, glob="*.txt", loader_cls=TextLoader, loader_kwargs={'encoding': 'utf-8'})
                docs.extend(loader_txt.load_and_split(text_splitter))
                loader_pdf = DirectoryLoader(PASTA_DOCS, glob="*.pdf", loader_cls=PyPDFLoader)
                docs.extend(loader_pdf.load_and_split(text_splitter))
                if docs:
                    os.makedirs(FAISS_CACHE_DIR, exist_ok=True)
                    vector_store = FAISS.from_documents(docs, embeddings)
                    vector_store.save_local(FAISS_CACHE_DIR)
                    with open(FAISS_HASH_FILE, "w", encoding="utf-8") as f:
                        f.write(hash_atual)
                    logger.info(f"FAISS atualizado com {len(docs)} fragmentos!")
            except Exception as e:
                logger.error(f"Erro ao indexar documentos: {_sanitizar_erro(e)}")


# ==========================================
# UTILITÁRIOS DO TELEGRAM E FORMATAÇÃO
# ==========================================
def limpar_latex(texto: str) -> str:
    texto = re.sub(r'\\\[|\\\]|\\\(|\\\)', '', texto)
    texto = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', texto)
    texto = texto.replace(r'\times', 'x').replace(r'\div', '÷').replace(r'\Rightarrow', '->').replace(r'\cdot', '·')
    texto = texto.replace('**', '')
    return texto.strip()


def _dividir_mensagem(texto: str, limite: int = 4000) -> list[str]:
    if len(texto) <= limite:
        return [texto]
    partes = []
    restante = texto
    while len(restante) > limite:
        corte = restante.rfind("\n\n", 0, limite)
        if corte == -1:
            corte = restante.rfind("\n", 0, limite)
        if corte == -1:
            corte = restante.rfind(" ", 0, limite)
        if corte == -1:
            corte = limite
        partes.append(restante[:corte].strip())
        restante = restante[corte:].strip()
    if restante:
        partes.append(restante)
    return partes


def enviar_acao_digitando(chat_id: int):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5
        )
    except Exception:
        pass


def enviar_mensagem(chat_id: int, texto: str, reply_markup: dict = None):
    texto_limpo = limpar_latex(texto)
    partes = _dividir_mensagem(texto_limpo, limite=4000)

    for idx, parte in enumerate(partes):
        payload = {"chat_id": chat_id, "text": parte}
        if reply_markup and idx == len(partes) - 1:
            payload["reply_markup"] = reply_markup

        for tentativa in range(2):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json=payload,
                    timeout=10
                )
                if resp.status_code == 200:
                    break
                if resp.status_code == 429 and tentativa == 0:
                    retry_after = 2
                    try:
                        retry_after = min(int(resp.json().get("parameters", {}).get("retry_after", 2)), 10)
                    except Exception:
                        pass
                    time.sleep(retry_after)
                    continue
                logger.error(f"Telegram sendMessage falhou com HTTP {resp.status_code}")
                break
            except Exception as e:
                logger.error(f"Erro de rede no sendMessage: {_sanitizar_erro(e)}")
                break


def editar_mensagem(chat_id: int, message_id: int, texto: str):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": limpar_latex(texto)[:4000]},
            timeout=10
        )
        if resp.status_code != 200:
            logger.error(f"Telegram editMessageText falhou com HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Erro de rede no editMessageText: {_sanitizar_erro(e)}")


def responder_callback(callback_id: str, texto: str = ""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": texto},
            timeout=5
        )
    except Exception:
        pass


def baixar_arquivo_telegram(file_id: str) -> tuple[bytes, str]:
    resp_info = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}", timeout=10)
    resp_info.raise_for_status()
    file_path = resp_info.json()["result"]["file_path"]
    resp_arq = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=20)
    resp_arq.raise_for_status()
    return resp_arq.content, file_path


# ==========================================
# DETECÇÃO DE MATÉRIA E RAG INTELIGENTE
# ==========================================
_RE_PALAVRAS_MAT = re.compile(
    r"\b(matem[aá]tica|multiplica[çc][aã]o|multiplicar|vezes|divis[aã]o|dividir|"
    r"soma|somar|subtra[çc][aã]o|subtrair|equa[çc][aã]o|equa[çc][oõ]es|"
    r"fra[çc][aã]o|fra[çc][oõ]es|porcentagem|porcento|regra de tr[eê]s|"
    r"tabuada|c[aá]lculo|calcular|raiz quadrada|pot[eê]ncia|mmc|mdc|"
    r"geometria|[aá]lgebra|polin[oô]mio|fatora[çc][aã]o|tri[aâ]ngulo|hipotenusa)\b",
    re.IGNORECASE
)
_RE_OPERACAO_MAT = re.compile(
    r"(?:\d\s*[+\-x×÷/*^]\s*\d)|(?:\d+(?:[.,]\d+)?\s*%)|(?:\b\d*[a-zA-Z]\s*[+\-=]\s*\d)|(?:\b\d+[a-zA-Z]\b)"
)
_RE_PALAVRAS_PORT = re.compile(
    r"\b(portugu[eê]s|crase|ortografia|acentua[çc][aã]o|concord[aâ]ncia|reg[eê]ncia|"
    r"pontua[çc][aã]o|porqu[eê]s?|sujeito|predicado|verbo|substantivo|adjetivo|"
    r"adv[eé]rbio|pronome|preposi[çc][aã]o|conjun[çc][aã]o|ora[çc][aã]o|sintaxe|"
    r"sem[aâ]ntica|interpreta[çc][aã]o de texto|reda[çc][aã]o|frase|plural|singular|"
    r"sin[oô]nimo|ant[oô]nimo|v[ií]rgula|gram[aá]tica)\b",
    re.IGNORECASE
)


def _classificar_texto_individual(texto: str) -> str | None:
    if not texto:
        return None
    sem_datas = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", texto)
    eh_mat = bool(_RE_PALAVRAS_MAT.search(sem_datas) or _RE_OPERACAO_MAT.search(sem_datas))
    eh_port = bool(_RE_PALAVRAS_PORT.search(sem_datas))
    if eh_mat and not eh_port:
        return "matematica"
    if eh_port and not eh_mat:
        return "portugues"
    if eh_mat and eh_port:
        return "matematica" if _RE_OPERACAO_MAT.search(sem_datas) else "portugues"
    return None


def detectar_materia_conversa(texto_atual: str, rows: list[tuple[str, str]], chat_id: str) -> bool:
    """Retorna True para Matemática, False para Português, olhando só mensagens do aluno."""
    classif_atual = _classificar_texto_individual(texto_atual)
    if classif_atual:
        salvar_materia_atual(chat_id, classif_atual)
        return classif_atual == "matematica"

    materia_salva = obter_materia_salva(chat_id)
    if materia_salva:
        return materia_salva == "matematica"

    for role, content in reversed(rows):
        if role == "user":
            c_hist = _classificar_texto_individual(content)
            if c_hist:
                salvar_materia_atual(chat_id, c_hist)
                return c_hist == "matematica"

    return False


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def chamar_llm_seguro(modelo, mensagens):
    return modelo.invoke(mensagens)


def _reescrever_consulta_rag(pergunta_usuario: str, rows: list[tuple[str, str]]) -> str | None:
    """Condensa histórico recente + pergunta curta em uma consulta autônoma para o FAISS."""
    if not rows:
        return pergunta_usuario if len(pergunta_usuario.split()) >= 3 else None

    if not llm_port:
        return pergunta_usuario if len(pergunta_usuario.split()) >= 3 else None

    ultimas = rows[-4:]
    resumo_hist = "\n".join([f"{'Aluno' if r == 'user' else 'Tutor'}: {c[:220]}" for r, c in ultimas])
    prompt_reescrita = (
        "Reescreva a última mensagem do aluno como uma pergunta ou tema de estudo único e autônomo "
        "para buscar em uma apostila escolar, usando o contexto abaixo. "
        "Se a mensagem for apenas 'não sei', 'ok', 'obrigado' ou não tiver tema buscável, responda APENAS: NENHUMA.\n\n"
        f"Histórico recente:\n{resumo_hist}\n\n"
        f"Última mensagem do aluno: {pergunta_usuario}\n\n"
        "Consulta autônoma:"
    )
    try:
        res = llm_port.invoke([HumanMessage(content=prompt_reescrita)])
        consulta = res.content.strip().strip('"')
        if not consulta or consulta.upper().startswith("NENHUMA"):
            return None
        return consulta
    except Exception:
        return pergunta_usuario if len(pergunta_usuario.split()) >= 3 else None


def gerar_resposta_inteligente(pergunta_usuario: str, chat_id: str, veio_de_audio: bool = False) -> str:
    resposta_bypass = interceptar_mensagem(pergunta_usuario)
    if resposta_bypass:
        return resposta_bypass

    if not llm_mat or not llm_port:
        return "O motor de IA está inicializando. Tente novamente em alguns segundos."

    rows = obter_historico_recente(chat_id, limite=10)
    eh_matematica = detectar_materia_conversa(pergunta_usuario, rows, chat_id)

    modelo_ativo = llm_mat if eh_matematica else llm_port
    diretriz_sistema = PROMPT_MATEMATICA if eh_matematica else PROMPT_PORTUGUES

    # Regra Anti-Loop
    frases_bloqueio = {
        "não sei", "nao sei", "não entendi", "nao entendi",
        "como funciona", "como funciona?", "como funciona isso?",
        "me ajuda", "explica", "pode explicar", "me dá a resposta", "me da a resposta"
    }
    p_norm = pergunta_usuario.strip().lower()
    msgs_usuario_recentes = [content for role, content in rows if role == "user"]
    travou_antes = any(any(f in m.lower() for f in frases_bloqueio) for m in msgs_usuario_recentes[-3:])
    travou_agora = any(f in p_norm for f in frases_bloqueio)

    regra_pedagogica = (
        "- O aluno já pediu ajuda anteriormente e está travado. NÃO faça mais perguntas socráticas. Entregue a resolução completa passo a passo de forma clara e didática, e ao final pergunte se ele quer tentar um exemplo parecido."
        if (travou_agora and travou_antes) else
        "- Se o aluno disser 'não sei' ou pedir como funciona, dê uma explicação prática e um exemplo simples para destravá-lo, sem repetir perguntas que ele já respondeu."
    )

    regras_extras = (
        "\n\nREGRAS OBRIGATÓRIAS:\n"
        "- Mantenha o foco EXCLUSIVAMENTE no assunto atual da conversa.\n"
        "- Nunca despeje conteúdos aleatórios do material de apoio se não responderem diretamente à dúvida.\n"
        f"{regra_pedagogica}\n"
        "- PROIBIDO usar LaTeX (como \\[ \\], \\frac, \\times, \\Rightarrow, $). Escreva contas em texto limpo (ex: 10 x 10 = 100 ou 5/10 = 8/x)."
    )

    if veio_de_audio:
        regras_extras += (
            "\n- AVISO DO SISTEMA: Esta mensagem veio de transcrição automática de áudio. "
            "NÃO avalie ortografia, acentuação ou pontuação; avalie apenas o conteúdo, raciocínio, concordância ou regência."
        )

    topicos_fracos = obter_topicos_fracos(chat_id)
    if topicos_fracos:
        lista_fracos = ", ".join([f"{top} ({mat})" for mat, top, _ in topicos_fracos])
        regras_extras += (
            f"\n- MEMÓRIA DO ALUNO: Nos quizzes recentes, este aluno apresentou dificuldade em: {lista_fracos}. "
            "Se surgir oportunidade natural na conversa, reforce esses conceitos ou sugira uma revisão."
        )

    # Gabarito determinístico (router.py)
    resposta_pronta, nota_gabarito = analisar_matematica(pergunta_usuario, msgs_usuario_recentes)
    if resposta_pronta:
        salvar_mensagem(chat_id, "user", pergunta_usuario)
        salvar_mensagem(chat_id, "bot", resposta_pronta)
        return resposta_pronta

    if nota_gabarito and eh_matematica:
        regras_extras += "\n- " + nota_gabarito

    mensagens = [SystemMessage(content=diretriz_sistema + regras_extras)]
    for role, content in rows:
        mensagens.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))

    # Busca RAG com consulta reescrita a partir do histórico
    contexto = ""
    if vector_store and p_norm not in frases_bloqueio:
        consulta_rag = _reescrever_consulta_rag(pergunta_usuario, rows)
        if consulta_rag:
            docs_com_score = vector_store.similarity_search_with_score(consulta_rag, k=2)
            if docs_com_score and docs_com_score[0][1] < 0.85:
                contexto = "\n\n".join([d.page_content for d, _ in docs_com_score])

    corpo_pergunta = (
        f"MATERIAL DE APOIO (use SOMENTE se responder diretamente à dúvida):\n{contexto}\n\nPERGUNTA DO ALUNO: {pergunta_usuario}"
        if contexto else f"PERGUNTA DO ALUNO: {pergunta_usuario}"
    )
    mensagens.append(HumanMessage(content=corpo_pergunta))

    try:
        res = chamar_llm_seguro(modelo_ativo, mensagens)
        resposta_bot = limpar_latex(res.content)
        salvar_mensagem(chat_id, "user", pergunta_usuario)
        salvar_mensagem(chat_id, "bot", resposta_bot)
        return resposta_bot
    except Exception as e:
        logger.error(f"Erro na inferência: {_sanitizar_erro(e)}")
        return "Tive uma instabilidade momentânea no servidor. Mande a mensagem novamente."


# ==========================================
# ÁUDIO, VISÃO E QUIZ VERIFICADO
# ==========================================
def processar_audio(chat_id: int, file_id: str):
    enviar_acao_digitando(chat_id)
    if not groq_client:
        enviar_mensagem(chat_id, "O módulo de áudio ainda está iniciando. Tente em alguns segundos.")
        return

    try:
        audio_bytes, file_path = baixar_arquivo_telegram(file_id)
        nome_arquivo = os.path.basename(file_path) or "audio.ogg"

        transcricao = groq_client.audio.transcriptions.create(
            file=(nome_arquivo, audio_bytes),
            model="whisper-large-v3-turbo",
            language="pt"
        )
        texto_transcrito = transcricao.text.strip()
        if not texto_transcrito:
            enviar_mensagem(chat_id, "Não consegui ouvir nada no áudio. Pode gravar novamente?")
            return

        logger.info(f"Áudio transcrito de {chat_id}")
        enviar_acao_digitando(chat_id)
        resposta = gerar_resposta_inteligente(texto_transcrito, str(chat_id), veio_de_audio=True)
        enviar_mensagem(chat_id, f"🎙️ Entendi: \"{texto_transcrito}\"\n\n{resposta}")
    except Exception as e:
        logger.error(f"Erro ao processar áudio: {_sanitizar_erro(e)}")
        enviar_mensagem(chat_id, "Não consegui processar seu áudio agora. Tente enviar novamente ou digite sua dúvida.")


def processar_imagem(chat_id: int, file_id: str, legenda: str):
    enviar_acao_digitando(chat_id)
    if not gemini_client:
        enviar_mensagem(chat_id, "O módulo de visão ainda está iniciando. Tente em alguns segundos.")
        return

    try:
        img_bytes, _ = baixar_arquivo_telegram(file_id)
        rows = obter_historico_recente(str(chat_id), limite=6)
        eh_matematica = detectar_materia_conversa(legenda, rows, str(chat_id))
        diretriz_base = PROMPT_MATEMATICA if eh_matematica else PROMPT_PORTUGUES

        instrucao_visao = (
            f"{diretriz_base}\n\n"
            "INSTRUÇÕES ADICIONAIS PARA ANÁLISE DE IMAGEM:\n"
            "- Analise a foto do exercício enviada pelo aluno levando em conta o histórico da conversa.\n"
            "- Identifique brevemente o enunciado da imagem e, se houver rascunho/resolução do aluno, aponte o que acertou e onde errou.\n"
            "- Siga a abordagem socrática (dê a dica para o próximo passo, ou explique passo a passo se o aluno pediu ajuda direta).\n"
            "- NUNCA use LaTeX (como \\[ \\], \\frac, \\times, $). Escreva contas em texto simples."
        )

        conteudos = []
        for role, content in rows:
            papel = "user" if role == "user" else "model"
            conteudos.append(types.Content(role=papel, parts=[types.Part.from_text(text=content)]))

        texto_pedido = f"Analise esta imagem do meu exercício. Comentário: {legenda}" if legenda else "Analise esta imagem do meu exercício."
        conteudos.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    types.Part.from_text(text=texto_pedido)
                ]
            )
        )

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=conteudos,
            config=types.GenerateContentConfig(
                system_instruction=instrucao_visao,
                temperature=0.2
            )
        )
        resposta_limpa = limpar_latex(response.text)
        salvar_mensagem(str(chat_id), "user", f"[Enviou foto de exercício] {legenda}".strip())
        salvar_mensagem(str(chat_id), "bot", resposta_limpa)
        enviar_mensagem(chat_id, resposta_limpa)
    except Exception as e:
        logger.error(f"Erro ao processar imagem: {_sanitizar_erro(e)}")
        enviar_mensagem(chat_id, "Não consegui ler essa imagem com clareza. Tente tirar uma foto mais iluminada ou enviar o texto da questão.")


def _gerar_e_verificar_questao(materia: str, trecho_base: str) -> dict | None:
    """Gera questão em JSON mode e faz uma 2ª chamada independente para validar o gabarito."""
    modelo_groq = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    prompt_criacao = (
        f"Crie 1 questão objetiva de nível escolar (Ensino Fundamental ou Médio) sobre {materia}.\n"
        + (f"Inspire-se neste trecho do material de estudo:\n{trecho_base}\n\n" if trecho_base else "")
        + "Responda APENAS com um objeto JSON válido com as chaves:\n"
        '{"topico": "Subtema curto (ex: Frações, Crase, Equação de 1º grau)", '
        '"pergunta": "Enunciado claro sem LaTeX", '
        '"opcoes": {"A": "...", "B": "...", "C": "...", "D": "..."}, '
        '"correta": "A", '
        '"explicacao": "Explicação objetiva de 1 a 2 linhas sem LaTeX"}'
    )

    for _ in range(2):
        try:
            comp_criacao = groq_client.chat.completions.create(
                model=modelo_groq,
                messages=[{"role": "user", "content": prompt_criacao}],
                response_format={"type": "json_object"},
                reasoning_effort="low",
                temperature=0.3,
                max_tokens=1500
            )
            dados = json.loads(comp_criacao.choices[0].message.content)
            op = dados.get("opcoes", {})
            correta_criador = str(dados.get("correta", "")).strip().upper()
            if correta_criador not in ("A", "B", "C", "D") or not all(k in op for k in ("A", "B", "C", "D")):
                continue

            # Segunda chamada independente para conferir o gabarito sem viés
            prompt_verificacao = (
                "Resolva a questão abaixo com atenção e identifique a única alternativa correta.\n"
                f"Pergunta: {dados['pergunta']}\n"
                f"A) {op['A']}\nB) {op['B']}\nC) {op['C']}\nD) {op['D']}\n\n"
                'Responda APENAS com JSON: {"resposta_verificada": "A"}'
            )
            comp_verif = groq_client.chat.completions.create(
                model=modelo_groq,
                messages=[{"role": "user", "content": prompt_verificacao}],
                response_format={"type": "json_object"},
                reasoning_effort="medium",
                temperature=0.0,
                max_tokens=1500
            )
            verif = json.loads(comp_verif.choices[0].message.content)
            correta_verif = str(verif.get("resposta_verificada", "")).strip().upper()

            if correta_criador == correta_verif:
                dados["correta"] = correta_criador
                return dados
            logger.warning(f"Gabarito divergente descartado no quiz ({correta_criador} vs {correta_verif})")
        except Exception as e:
            logger.warning(f"Tentativa de geração de quiz falhou: {_sanitizar_erro(e)}")

    return None


def gerar_quiz(chat_id: int, comando: str):
    enviar_acao_digitando(chat_id)
    if not groq_client:
        enviar_mensagem(chat_id, "O motor de IA ainda está iniciando.")
        return

    cmd = comando.lower()
    if "mat" in cmd:
        materia = "Matemática"
    elif "port" in cmd:
        materia = "Português"
    else:
        materia = random.choice(["Matemática", "Português"])

    trecho_base = ""
    if vector_store:
        try:
            docs_aleatorios = vector_store.similarity_search(materia, k=3)
            if docs_aleatorios:
                trecho_base = random.choice(docs_aleatorios).page_content
        except Exception:
            pass

    dados = _gerar_e_verificar_questao(materia, trecho_base)
    if not dados:
        enviar_mensagem(chat_id, "Não consegui montar uma questão validada agora. Mande /quiz novamente!")
        return

    quiz_id = f"q_{secrets.token_hex(4)}"
    topico = str(dados.get("topico", "Geral")).strip()[:60] or "Geral"
    op = dados["opcoes"]

    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO quizzes (quiz_id, chat_id, materia, topico, pergunta, opcoes_json, correta, explicacao, respondido) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (quiz_id, str(chat_id), materia, topico, dados["pergunta"], json.dumps(op, ensure_ascii=False), dados["correta"], dados["explicacao"])
    )
    conn.commit()
    conn.close()

    texto_msg = (
        f"🎯 QUIZ DE {materia.upper()} ({topico})\n\n"
        f"{dados['pergunta']}\n\n"
        f"A) {op['A']}\n"
        f"B) {op['B']}\n"
        f"C) {op['C']}\n"
        f"D) {op['D']}"
    )

    teclado = {
        "inline_keyboard": [[
            {"text": "A", "callback_data": f"quiz:{quiz_id}:A"},
            {"text": "B", "callback_data": f"quiz:{quiz_id}:B"},
            {"text": "C", "callback_data": f"quiz:{quiz_id}:C"},
            {"text": "D", "callback_data": f"quiz:{quiz_id}:D"}
        ]]
    }
    enviar_mensagem(chat_id, texto_msg, reply_markup=teclado)


def processar_resposta_quiz(callback: dict):
    callback_id = callback.get("id")
    data_str = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    partes = data_str.split(":")
    if len(partes) != 3 or partes[0] != "quiz":
        responder_callback(callback_id)
        return

    _, quiz_id, escolha = partes

    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()

    # Atualização atômica: impede clique duplo de contar duas vezes
    c.execute("UPDATE quizzes SET respondido = 1 WHERE quiz_id = ? AND respondido = 0", (quiz_id,))
    if c.rowcount == 0:
        conn.close()
        responder_callback(callback_id, "Você já respondeu essa questão!")
        return

    c.execute("SELECT materia, topico, pergunta, opcoes_json, correta, explicacao FROM quizzes WHERE quiz_id = ?", (quiz_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        responder_callback(callback_id, "Quiz expirado.")
        return

    materia, topico, pergunta, opcoes_json, correta, explicacao = row
    acertou = (escolha == correta)

    c.execute("INSERT OR IGNORE INTO placar (chat_id, acertos, total) VALUES (?, 0, 0)", (str(chat_id),))
    c.execute(
        "UPDATE placar SET acertos = acertos + ?, total = total + 1 WHERE chat_id = ?",
        (1 if acertou else 0, str(chat_id))
    )

    c.execute(
        "INSERT OR IGNORE INTO desempenho_topicos (chat_id, materia, topico, acertos, erros) VALUES (?, ?, ?, 0, 0)",
        (str(chat_id), materia, topico)
    )
    if acertou:
        c.execute(
            "UPDATE desempenho_topicos SET acertos = acertos + 1 WHERE chat_id = ? AND materia = ? AND topico = ?",
            (str(chat_id), materia, topico)
        )
    else:
        c.execute(
            "UPDATE desempenho_topicos SET erros = erros + 1 WHERE chat_id = ? AND materia = ? AND topico = ?",
            (str(chat_id), materia, topico)
        )

    c.execute("SELECT acertos, total FROM placar WHERE chat_id = ?", (str(chat_id),))
    acertos, total = c.fetchone()
    conn.commit()
    conn.close()

    opcoes = json.loads(opcoes_json)
    status = "✅ ACERTOU!" if acertou else f"❌ ERROU! Você marcou {escolha}."
    responder_callback(callback_id, "Parabéns!" if acertou else f"A correta era {correta}")

    texto_final = (
        f"🎯 QUIZ DE {materia.upper()} ({topico})\n\n"
        f"{pergunta}\n\n"
        f"{status}\n"
        f"Resposta correta: {correta}) {opcoes.get(correta, '')}\n\n"
        f"💡 Explicação: {explicacao}\n\n"
        f"📊 Seu placar: {acertos}/{total} acertos (mande /quiz para outra)"
    )
    editar_mensagem(chat_id, message_id, texto_final)

    # Salva o quiz completo no histórico para o bot entender perguntas seguintes ("por que a B está errada?")
    resumo_historico = (
        f"[Quiz de {materia} - {topico}] Pergunta: {pergunta} | "
        f"Opções: A) {opcoes.get('A')} | B) {opcoes.get('B')} | C) {opcoes.get('C')} | D) {opcoes.get('D')} | "
        f"Aluno marcou: {escolha} | Correta: {correta} | Explicação: {explicacao}"
    )
    salvar_mensagem(str(chat_id), "bot", resumo_historico)
    salvar_materia_atual(str(chat_id), "matematica" if materia == "Matemática" else "portugues")


# ==========================================
# ORQUESTRADOR DO WEBHOOK (ASSÍNCRONO)
# ==========================================
def processar_update_telegram(data: dict):
    try:
        if "callback_query" in data:
            processar_resposta_quiz(data["callback_query"])
            return

        message = data.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return

        if "voice" in message or "audio" in message:
            file_id = (message.get("voice") or message.get("audio")).get("file_id")
            logger.info(f"Áudio recebido de {chat_id}")
            processar_audio(chat_id, file_id)
            return

        if "photo" in message:
            file_id = message["photo"][-1]["file_id"]
            legenda = message.get("caption", "")
            logger.info(f"Foto recebida de {chat_id}")
            processar_imagem(chat_id, file_id, legenda)
            return

        texto_usuario = message.get("text", "").strip()
        if not texto_usuario:
            return

        logger.info(f"Texto de {chat_id}: {texto_usuario}")
        cmd = texto_usuario.lower()

        if cmd in ("/start", "/ajuda", "/help"):
            boas_vindas = (
                "Olá! Sou o Educa Bot, seu tutor virtual de Matemática e Português.\n\n"
                "Como você pode estudar comigo:\n"
                "💬 Texto: Mande qualquer dúvida, frase ou conta\n"
                "🎙️ Áudio: Segure o microfone e pergunte falando\n"
                "📸 Foto: Tire foto de um exercício do caderno ou livro\n"
                "🎯 /quiz: Responda questões com botões interativos\n"
                "📊 /placar: Veja seu desempenho e pontos a revisar\n"
                "🧹 /limpar: Zera nossa conversa atual para mudar de assunto"
            )
            enviar_mensagem(chat_id, boas_vindas)
            return

        if cmd == "/limpar":
            limpar_historico(str(chat_id))
            enviar_mensagem(chat_id, "🧹 Histórico limpo! Pode mandar sua próxima dúvida.")
            return

        if cmd == "/placar":
            conn = sqlite3.connect(DB_SQLITE)
            c = conn.cursor()
            c.execute("SELECT acertos, total FROM placar WHERE chat_id = ?", (str(chat_id),))
            row = c.fetchone()
            conn.close()
            if not row or row[1] == 0:
                enviar_mensagem(chat_id, "Você ainda não respondeu nenhum quiz. Mande /quiz para começar!")
            else:
                pct = int((row[0] / row[1]) * 100)
                msg_placar = f"📊 Seu placar: {row[0]} acertos em {row[1]} questões ({pct}% de aproveitamento)."
                fracos = obter_topicos_fracos(str(chat_id))
                if fracos:
                    temas = ", ".join([f"{t} ({m})" for m, t, _ in fracos])
                    msg_placar += f"\n\n📌 Tópicos recomendados para revisão: {temas}."
                enviar_mensagem(chat_id, msg_placar)
            return

        if cmd.startswith("/quiz"):
            gerar_quiz(chat_id, texto_usuario)
            return

        enviar_acao_digitando(chat_id)
        resposta_final = gerar_resposta_inteligente(texto_usuario, str(chat_id))
        enviar_mensagem(chat_id, resposta_final)

    except Exception as e:
        logger.error(f"Erro geral no processamento do update: {_sanitizar_erro(e)}")


@app.get("/")
def healthcheck():
    return {"status": "online", "bot": "Educa Bot Multimodal", "version": "4.0"}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    if TELEGRAM_WEBHOOK_SECRET:
        secret_recebido = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret_recebido != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Secret token inválido")

    data = await request.json()
    update_id = data.get("update_id")
    if not registrar_update_id(update_id):
        return {"status": "duplicate_ignored"}

    background_tasks.add_task(processar_update_telegram, data)
    return {"status": "ok"}