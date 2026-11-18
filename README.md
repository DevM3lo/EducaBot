# Educa Bot — Tutor Virtual Multimodal (Matemática e Português)

Tutor educacional inteligente integrado ao **Telegram**, projetado para auxiliar estudantes do Ensino Fundamental e Médio por meio do **Método Socrático** aliado a recursos multimodais, busca em materiais de estudo (RAG) e simulados interativos.

## Funcionalidades

- **Tutoria Socrática com Anti-Loop:** Conduz o aluno passo a passo por meio de perguntas e dicas sem entregar respostas prontas de imediato, mas destrava e apresenta a resolução completa caso o estudante demonstre dificuldade repetida.
- **Avaliador Matemático Determinístico (`router.py`):** Utiliza `ast` e `Fraction` (sem `eval`) para calcular expressões aritméticas com precisão exata, validar respostas numéricas instantaneamente sem custo de LLM e fornecer gabarito conferido ao modelo.
- **Entrada Multimodal (Áudio e Visão):**
  - **Áudio:** Transcrição automática de mensagens de voz do Telegram via `whisper-large-v3-turbo` (Groq).
  - **Visão:** Leitura e correção comentada de fotos de caderno, livros ou equações manuscritas via `gemini-2.5-flash` (Google Gemini).
- **Simulados Interativos (`/quiz`):** Gera questões de múltipla escolha em JSON Mode com **dupla verificação independente de gabarito**, botões inline (`A`, `B`, `C`, `D`), proteção contra clique duplo e histórico de desempenho por tópico (`/placar`).
- **Base de Conhecimento (RAG com FAISS):** Indexa arquivos `.txt` e `.pdf` da pasta `docs/` com embeddings `models/gemini-embedding-001`, reescrita contextual de perguntas e cache persistente validado por hash SHA-256.

## Arquitetura e Stack

| Camada | Tecnologia |
| :--- | :--- |
| **API & Webhook** | Python, FastAPI, Uvicorn (`BackgroundTasks` + deduplicação por `update_id`) |
| **Raciocínio & Áudio** | Groq API (`openai/gpt-oss-120b` e `whisper-large-v3-turbo`) |
| **Visão & Embeddings** | Google GenAI (`gemini-2.5-flash` e `models/gemini-embedding-001`) |
| **Banco Vetorial (RAG)** | LangChain + FAISS (cache em disco com invalidação por SHA-256) |
| **Persistência** | SQLite (histórico de conversas, estado da matéria, quizzes e placar por tópico) |
| **Hospedagem** | Railway (deploy contínuo via GitHub + Volume persistente em `/data`) |

## Estrutura do Projeto

```text
├── api.py              # Servidor FastAPI, webhook do Telegram, RAG, áudio, visão e quiz
├── router.py           # Interceptação de saudações e motor determinístico de matemática
├── prompts.py          # Diretrizes pedagógicas socráticas de Matemática e Português
├── docs/               # Materiais de apoio (.txt e .pdf) indexados pelo FAISS
├── requirements.txt    # Dependências Python
└── README.md           # Documentação do projeto