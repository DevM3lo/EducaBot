from fastapi import FastAPI, Request
from pydantic import BaseModel
import os
import requests
import sqlite3
import logging
from tenacity import retry, stop_after_attempt, wait_fixed

from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_community.document_loaders import DirectoryLoader, TextLoader, PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

# Importando nossos módulos novos
from router import interceptar_mensagem
from prompts import PROMPT_MATEMATICA, PROMPT_PORTUGUES

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

os.environ["TELEGRAM_BOT_TOKEN"] = "blep"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

app = FastAPI(title="Educa Bot - Enterprise Local", version="Final-Pro")
PASTA_DOCS = "docs"
DB_SQLITE = "historico_chat.db"

vector_store = None
llm = None

def init_db():
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
    logger.info("Inicializando infraestrutura local...")
    
    try:
        llm = ChatOllama(model="llama3.1", temperature=0.0, num_ctx=4096, num_predict=250)
        logger.info("Llama 3.1 carregado com sucesso na GPU.")
    except Exception as e:
        logger.error(f"Erro ao carregar LLM: {e}")

    try:
        embeddings = OllamaEmbeddings(model="nomic-embed-text")
        logger.info("Embeddings conectados.")
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
        except Exception as e: 
            logger.error(f"Erro ao ler documentos: {e}")
        
        if docs:
            vector_store = FAISS.from_documents(docs, embeddings)
            logger.info(f"FAISS populado com {len(docs)} fragmentos!")

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def chamar_llm_seguro(mensagens):
    return llm.invoke(mensagens)

def gerar_resposta_inteligente(pergunta_usuario, chat_id="padrao"):
    # 1. Roteamento de Bypass (Zero custo computacional para saudações/lixo)
    resposta_bypass = interceptar_mensagem(pergunta_usuario)
    if resposta_bypass:
        return resposta_bypass

    if not vector_store:
        return "Estou organizando os materiais de estudo. Tente novamente em instantes."
    
    # 2. Busca no FAISS com pontuação de relevância (Evita alucinação por contexto fraco)
    docs_com_score = vector_store.similarity_search_with_score(pergunta_usuario, k=3)
    
    contexto = ""
    # Se a pontuação indicar alta relevância, usamos o contexto. (Menor score no FAISS = maior similaridade)
    if docs_com_score and docs_com_score[0][1] < 1.2: 
        contexto = "\n\n".join([d.page_content for d, score in docs_com_score])

    # 3. Escolha dinâmica de Prompt (Matemática vs Português baseada em heurística simples)
    tem_matematica = any(char in pergunta_usuario for char in "+-*/=0123456789") or "quanto" in pergunta_usuario.lower()
    diretriz_sistema = PROMPT_MATEMATICA if tem_matematica else PROMPT_PORTUGUES

    mensagens = [SystemMessage(content=diretriz_sistema)]
    
    # 4. Recuperação estruturada do Histórico via SQLite
    conn = sqlite3.connect(DB_SQLITE)
    c = conn.cursor()
    c.execute("SELECT role, content FROM (SELECT role, content, timestamp FROM conversas WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 6) ORDER BY timestamp ASC", (str(chat_id),))
    rows = c.fetchall()
    conn.close()
    
    for role, content in rows:
        if role == "user":
            mensagens.append(HumanMessage(content=content))
        elif role == "bot":
            mensagens.append(AIMessage(content=content))

    # Injeta o contexto recuperado (se houver) junto à pergunta
    corpo_pergunta = f"CONTEXTO RELEVANTE:\n{contexto}\n\nPERGUNTA DO ALUNO: {pergunta_usuario}" if contexto else f"PERGUNTA DO ALUNO: {pergunta_usuario}"
    mensagens.append(HumanMessage(content=corpo_pergunta))
    
    try:
        res = chamar_llm_seguro(mensagens)
        resposta_bot = res.content
        
        salvar_mensagem(chat_id, "user", pergunta_usuario)
        salvar_mensagem(chat_id, "bot", resposta_bot)
        
        return resposta_bot
    except Exception as e:
        logger.error(f"Erro na inferência local: {e}")
        return "Tive um soluço técnico aqui no servidor local. Mande a mensagem novamente."

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