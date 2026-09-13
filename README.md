# EducaBot - Assistente Virtual Socrático (RAG) 🤖📚

Um assistente virtual educacional desenvolvido com **Python** e **FastAPI**, utilizando arquitetura **RAG (Retrieval-Augmented Generation)** para auxiliar alunos no aprendizado de Matemática e Português através do Método Socrático.

## 🚀 Tecnologias Utilizadas

Este projeto foi construído utilizando um ecossistema moderno de IA e Back-end:

*   **FastAPI:** Framework web rápido para a construção da API.
*   **LangChain:** Framework para orquestração da inteligência artificial.
*   **Cohere:** LLM (Large Language Model) utilizado para geração de texto e embeddings.
*   **FAISS (Facebook AI Similarity Search):** Banco de dados vetorial local para busca rápida de similaridade.
*   **Uvicorn:** Servidor ASGI para rodar a aplicação.

## ⚙️ Funcionalidades

*   **Método Socrático:** A IA é instruída a nunca dar a resposta diretamente, mas sim guiar o aluno através de perguntas reflexivas.
*   **Busca Vetorial Cirúrgica:** O sistema fatia (`RecursiveCharacterTextSplitter`) e lê documentos locais específicos para garantir zero alucinações nas respostas.
*   **Memória de Contexto:** O bot mantém o histórico da conversa, permitindo interações contínuas e lógicas.
*   **Escopo Fechado:** Foco otimizado para demonstrações precisas em regras de Português e equações de Matemática.
