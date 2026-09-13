# router.py
import re

def interceptar_mensagem(texto: str):
    """
    Analisa a mensagem do usuário antes de enviar para a IA.
    Retorna uma resposta pronta (string) se for um caso tratado, ou None se precisar ir para o LLM.
    """
    texto_limpo = texto.strip().lower()
    
    # 1. Tratamento de Saudações e Identidade
    saudacoes = ["oi", "olá", "ola", "tudo bem", "bom dia", "boa tarde", "boa noite", "e aí", "eae"]
    if texto_limpo in saudacoes or "quem é você" in texto_limpo or "qual seu nome" in texto_limpo:
        return "Olá! Sou o Educa Bot, seu tutor virtual. Qual é a sua dúvida de Matemática ou Português hoje?"
    
    # 2. Proteção de Escopo (Esportes, Política, etc)
    temas_proibidos = ["corinthians", "futebol", "flamengo", "politica", "fofoca", "filme", "jogo do"]
    if any(tema in texto_limpo for tema in temas_proibidos):
        return "Sou um tutor focado exclusivamente nos estudos. Como posso ajudar com Matemática ou Português hoje?"

    return None