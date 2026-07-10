from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import os
import requests
import json

from langchain_cohere import ChatCohere, CohereEmbeddings
from langchain_community.document_loaders import DirectoryLoader, TextLoader, PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.prompts import PromptTemplate

os.environ["COHERE_API_KEY"] = "SU_COHERE_API_KEY_AQUI"
TELEGRAM_TOKEN = "SU_TELEGRAM_BOT_TOKEN_AQUI"

app = FastAPI(title="Educa Bot - Tutor IA", version="Final")
PASTA_DOCS = "docs"
PASTA_DB = "faiss_index"

vector_store = None
llm = None
historico_conversas = {}

@app.on_event("startup")
def startup_event():
    global vector_store, llm
    print(">>> Inicializando Cérebro do Educa Bot 24/7...")
    
    try:
        llm = ChatCohere(
            model="command-r-08-2024", 
            temperature=0.4
        )
    except:
        print("Erro ao conectar Cohere Chat")

    try:
        embeddings = CohereEmbeddings(
            model="embed-multilingual-v3.0"
        )
    except:
        print("Erro nas chaves da Cohere. Verifique o COHERE_API_KEY.")
        embeddings = None

    if os.path.exists(PASTA_DOCS) and embeddings:
        print(">>> Indexando material de estudo...")
        docs = []
        try:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000, 
                chunk_overlap=150
            )
            
            loader_txt = DirectoryLoader(PASTA_DOCS, glob="*.txt", loader_cls=TextLoader, loader_kwargs={'encoding': 'utf-8'})
            docs.extend(loader_txt.load_and_split(text_splitter))
            
            loader_pdf = DirectoryLoader(PASTA_DOCS, glob="*.pdf", loader_cls=PyPDFLoader)
            docs.extend(loader_pdf.load_and_split(text_splitter))
            
        except Exception as e: 
            print(f"Erro ao ler arquivos: {e}")
            pass
        
        if docs:
            vector_store = FAISS.from_documents(docs, embeddings)
            print(f">>> Memória carregada com {len(docs)} trechos de estudo!")
        else:
            print(">>> AVISO: Nenhum documento lido. Coloque textos ou PDFs na pasta 'docs'.")

def gerar_resposta_rag(pergunta_usuario, chat_id="padrao"):
    if not vector_store:
        return "Desculpe, estou organizando meus livros. Tente em 1 minuto."
    
    if chat_id not in historico_conversas:
        historico_conversas[chat_id] = []
        
    historico_recente = historico_conversas[chat_id][-6:]
    texto_historico = "\n".join(historico_recente) if historico_recente else "Nenhuma conversa anterior."
    
    docs = vector_store.similarity_search(pergunta_usuario, k=4)
    contexto = "\n\n".join([d.page_content for d in docs])
    
    template = """
    [DIRETRIZ DE SISTEMA: PRIORIDADE MÁXIMA]
    Você é o Educa Bot, um Tutor Socrático brilhante e encorajador. 
    Seu objetivo NÃO é dar a resposta, mas sim guiar o raciocínio do aluno para que ELE próprio descubra a solução.

    REGRAS INQUEBRÁVEIS:
    1. PROIBIÇÃO DE RESPOSTA DIRETA: Nunca entregue o resultado final de uma equação, problema ou questão de gramática de imediato. Mesmo que o aluno peça a resposta direta, recuse educadamente e devolva o problema em partes.
    2. PASSO A PASSO: Se o problema for complexo, ajude a resolver apenas o passo atual e pare.
    3. OBRIGAÇÃO DA PERGUNTA FINAL: Você DEVE obrigatoriamente terminar TODAS as suas mensagens com UMA ÚNICA pergunta clara e direta que force o aluno a pensar no próximo passo. 
    4. ANCORAGEM: Utilize as informações do CONTEXTO DE ESTUDO para basear suas explicações.

    CONTEXTO DE ESTUDO (Material da aula):
    {contexto}
    
    HISTÓRICO RECENTE DA CONVERSA:
    {historico}
    
    PERGUNTA ATUAL DO ALUNO: {pergunta}
    
    ATENÇÃO: Lembre-se da regra 3. Termine sua resposta com uma pergunta instigante!
    SUA RESPOSTA COMO TUTOR:"""
    
    prompt = template.format(contexto=contexto, historico=texto_historico, pergunta=pergunta_usuario)
    
    try:
        res = llm.invoke(prompt)
        resposta_bot = res.content
        
        historico_conversas[chat_id].append(f"Aluno: {pergunta_usuario}")
        historico_conversas[chat_id].append(f"Tutor: {resposta_bot}")
        
        return resposta_bot
    except Exception as e:
        return f"Erro temporário na IA: {e}"

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    try:
        message = data.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        texto_usuario = message.get("text", "")

        if not chat_id or not texto_usuario:
            return {"status": "ignored"} 

        print(f"Dúvida recebida do aluno {chat_id}: {texto_usuario}")

        if texto_usuario == "/start":
            resposta_final = """Olá! Sou o Educa Bot, seu Tutor Virtual. 📚🧠
            
Estou aqui para te ajudar a estudar Matemática, Português ou conversar sobre o impacto da Inteligência Artificial na Educação.

Lembre-se: eu não vou te dar as respostas prontas! Nós vamos raciocinar e aprender juntos. 

Mande a sua dúvida ou equação para começarmos!"""
        else:
            resposta_final = gerar_resposta_rag(texto_usuario, chat_id)

        url_envio = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": resposta_final
        }
        resposta_telegram = requests.post(url_envio, json=payload)
        print(f"Status do envio para o Telegram: {resposta_telegram.status_code} - {resposta_telegram.text}")

    except Exception as e:
        print(f"Erro no processamento: {e}")

    return {"status": "ok"}

class PerguntaTeste(BaseModel):
    texto: str

@app.post("/testar_tutor")
def testar_tutor(request: PerguntaTeste):
    resposta = gerar_resposta_rag(request.texto)
    return {"resposta": resposta}

@app.get("/")
def health_check():
    return {"status": "Estou vivo e rodando 24/7!", "servico": "Educa Bot"}