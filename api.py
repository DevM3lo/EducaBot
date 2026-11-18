from fastapi import FastAPI, Request
import os
import requests
import sqlite3
import logging
from tenacity import retry, stop_after_attempt, wait_fixed

from langchain_groq import ChatGroq
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_community.document_loaders import DirectoryLoader, TextLoader, PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from router import interceptar_mensagem
from prompts import PROMPT_MATEMATICA, PROMPT_PORTUGUES

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PASTA_DOCS = "docs"
DB_SQLITE = os.getenv("DB_PATH", "historico_chat.db")

app = FastAPI(title="Educa Bot - Cloud", version="2.0")

vector_store = None
llm = None

def init_db():
    db_dir = os.path.dirname(DB_SQLITE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS conversas
                 (chat_id TEXT, role TEXT, content TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def salvar_mensagem(chat_id, role, content):
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute("INSERT INTO conversas (chat_id, role, content) VALUES (?, ?, ?)", (str(chat_id), role, content))
    conn.commit()
    conn.close()

init_db()

@app.on_event("startup")
def startup_event():
    global vector_store, llm
    logger.info("Inicializando infraestrutura na nuvem...")

    try:
        modelo_groq = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        llm = ChatGroq(model=modelo_groq, temperature=0.1, max_tokens=1024)
        logger.info(f"Modelo {modelo_groq} (Groq) conectado com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao conectar Groq: {e}")

    try:
        embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
        logger.info("Embeddings (Google Gemini) conectados.")
    except Exception as e:
        logger.error(f"Erro nos Embeddings: {e}")
        embeddings = None

    if os.path.exists(PASTA_DOCS) and embeddings:
        logger.info("Indexando materiais...")
        docs = []
        try:
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=600, chunk_overlap=100)
            loader_txt = DirectoryLoader(PASTA_DOCS, glob="*.txt", loader_cls=TextLoader, loader_kwargs={'encoding': 'utf-8'})
            docs.extend(loader_txt.load_and_split(text_splitter))
            loader_pdf = DirectoryLoader(PASTA_DOCS, glob="*.pdf", loader_cls=PyPDFLoader)
            docs.extend(loader_pdf.load_and_split(text_splitter))
            if docs:
                vector_store = FAISS.from_documents(docs, embeddings)
                logger.info(f"FAISS populado com {len(docs)} fragmentos!")
        except Exception as e:
            logger.error(f"Erro ao ler ou indexar documentos: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def chamar_llm_seguro(mensagens):
    return llm.invoke(mensagens)

def detectar_matematica(texto: str) -> bool:
    palavras_mat = [
        "quanto", "multiplica", "vezes", "divid", "soma", "subtra",
        "equação", "equacao", "fração", "fracao", "porcentagem",
        "regra de três", "regra de tres", "tabuada", "número", "numero", "calcul"
    ]
    t = texto.lower()
    tem_simbolo = any(char in t for char in "+-*/=0123456789×÷%")
    tem_palavra = any(p in t for p in palavras_mat)
    return tem_simbolo or tem_palavra

def gerar_resposta_inteligente(pergunta_usuario, chat_id="padrao"):
    resposta_bypass = interceptar_mensagem(pergunta_usuario)
    if resposta_bypass:
        return resposta_bypass

    if not llm:
        return "O motor de IA está inicializando. Tente novamente em alguns segundos."

    # 1. Recupera histórico recente antes de decidir a matéria e o RAG
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM (SELECT role, content, timestamp FROM conversas WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 6) ORDER BY timestamp ASC",
        (str(chat_id),)
    )
    rows = c.fetchall()
    conn.close()

    # 2. Mantém a matéria da conversa se a mensagem atual for continuação curta ("não sei", "como funciona?")
    texto_recente = pergunta_usuario + " " + " ".join([content for _, content in rows[-2:]])
    eh_matematica = detectar_matematica(texto_recente)
    diretriz_sistema = PROMPT_MATEMATICA if eh_matematica else PROMPT_PORTUGUES

    regras_extras = (
        "\n\nREGRAS OBRIGATÓRIAS DE COMPORTAMENTO:\n"
        "- Mantenha o foco EXCLUSIVAMENTE no assunto que o aluno está perguntando agora.\n"
        "- Nunca despeje conteúdos aleatórios do material de apoio se não tiverem relação direta com a dúvida atual.\n"
        "- Se o aluno disser 'não sei' ou pedir como funciona, dê uma dica prática e objetiva para ele avançar, sem repetir perguntas que ele já respondeu.\n"
        "- NUNCA use código LaTeX (como \\[ \\], \\frac, \\times, \\Rightarrow). Escreva contas com texto simples (ex: 10 x 10 = 100 ou 5/10 = 8/x), pois o Telegram não renderiza LaTeX."
    )

    mensagens = [SystemMessage(content=diretriz_sistema + regras_extras)]

    for role, content in rows:
        if role == "user":
            mensagens.append(HumanMessage(content=content))
        elif role == "bot":
            mensagens.append(AIMessage(content=content))

    # 3. Busca no FAISS apenas se a pergunta tiver substância (evita puxar lixo em "não sei" / "como funciona isso?")
    contexto = ""
    continuacoes_curtas = {"não sei", "nao sei", "como funciona isso?", "como funciona isso", "como funciona?", "não entendi", "nao entendi", "explica", "me ajuda"}
    if vector_store and pergunta_usuario.strip().lower() not in continuacoes_curtas and len(pergunta_usuario.split()) >= 3:
        docs_com_score = vector_store.similarity_search_with_score(pergunta_usuario, k=2)
        if docs_com_score and docs_com_score[0][1] < 0.85:
            contexto = "\n\n".join([d.page_content for d, score in docs_com_score])

    corpo_pergunta = (
        f"MATERIAL DE APOIO (use SOMENTE se responder diretamente à dúvida abaixo):\n{contexto}\n\nPERGUNTA DO ALUNO: {pergunta_usuario}"
        if contexto else f"PERGUNTA DO ALUNO: {pergunta_usuario}"
    )
    mensagens.append(HumanMessage(content=corpo_pergunta))

    try:
        res = chamar_llm_seguro(mensagens)
        resposta_bot = res.content

        salvar_mensagem(chat_id, "user", pergunta_usuario)
        salvar_mensagem(chat_id, "bot", resposta_bot)

        return resposta_bot
    except Exception as e:
        logger.error(f"Erro na inferência: {e}")
        return "Tive uma instabilidade momentânea no servidor. Mande a mensagem novamente."

@app.get("/")
def healthcheck():
    return {"status": "online", "bot": "Educa Bot"}

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    try:
        message = data.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        texto_usuario = message.get("text", "")

        if not chat_id or not texto_usuario:
            return {"status": "ignored"}

        logger.info(f"Mensagem de {chat_id}: {texto_usuario}")

        if texto_usuario == "/start":
            resposta_final = "Olá! Sou o Educa Bot, seu tutor virtual. Mande sua dúvida de Matemática ou Português!"
        else:
            resposta_final = gerar_resposta_inteligente(texto_usuario, chat_id)

        url_envio = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url_envio, json={"chat_id": chat_id, "text": resposta_final})

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")

    return {"status": "ok"}