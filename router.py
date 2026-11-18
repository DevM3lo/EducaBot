
# router.py
import ast
import re
import unicodedata
from fractions import Fraction
from typing import Optional
 
RESPOSTA_SAUDACAO = (
    "Olá! Sou o Educa Bot, seu tutor virtual. "
    "Qual é a sua dúvida de Matemática ou Português hoje?"
)
RESPOSTA_CORRETO = "Correto! Parabéns! Qual é a sua próxima dúvida?"
 
 
# ==========================================
# 1. SAUDAÇÕES E IDENTIDADE (resposta pronta, sem gastar LLM)
# ==========================================
_RE_SAUDACAO = re.compile(
    r"^(tudo bem|td bem|oi+e?|ola+|eae|e ai|opa|bom dia|boa tarde|boa noite)"
    r"(\s+(tudo bem|td bem|tudo bom|tudo certo))?$"
)
_RE_IDENTIDADE = re.compile(
    r"\b(quem e voce|quem e vc|qual (e )?(o )?seu nome|como (voce|vc) se chama)\b"
)
 
 
def _normalizar(texto: str) -> str:
    """Minúsculas, sem acento e sem pontuação: 'Olá, tudo bem?!' -> 'ola tudo bem'."""
    t = unicodedata.normalize("NFD", texto.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())
 
 
def interceptar_mensagem(texto: str):
    """
    Retorna uma resposta pronta (str) se a mensagem for só saudação/identidade,
    ou None para seguir ao LLM.
    O controle de escopo (assuntos fora de Matemática/Português) fica nos prompts:
    uma lista de palavras bloqueava exercícios legítimos ("o Corinthians marcou 3 gols...").
    """
    t = _normalizar(texto)
    if _RE_SAUDACAO.match(t) or _RE_IDENTIDADE.search(t):
        return RESPOSTA_SAUDACAO
    return None
 
 
# ==========================================
# 2. GABARITO DETERMINÍSTICO PARA CONTAS SIMPLES
# ==========================================
_LIMITE_EXPR = 80
_LIMITE_VALOR = 10 ** 15
 
 
def _avaliar(no) -> Fraction:
    """Avalia a árvore com Fraction (resultado exato, sem eval)."""
    if isinstance(no, ast.Expression):
        return _avaliar(no.body)
    if isinstance(no, ast.Constant) and isinstance(no.value, (int, float)) and not isinstance(no.value, bool):
        return Fraction(str(no.value))
    if isinstance(no, ast.UnaryOp) and isinstance(no.op, (ast.USub, ast.UAdd)):
        v = _avaliar(no.operand)
        return -v if isinstance(no.op, ast.USub) else v
    if isinstance(no, ast.BinOp):
        a, b = _avaliar(no.left), _avaliar(no.right)
        if isinstance(no.op, ast.Add):
            return a + b
        if isinstance(no.op, ast.Sub):
            return a - b
        if isinstance(no.op, ast.Mult):
            return a * b
        if isinstance(no.op, ast.Div):
            return a / b  # ZeroDivisionError é tratado por quem chama
        if isinstance(no.op, ast.Pow):
            if b.denominator != 1 or abs(b) > 10 or abs(a) > 10 ** 6:
                raise ValueError("potência fora do limite")
            return a ** int(b)
    raise ValueError("expressão não suportada")
 
 
def _formatar(v: Fraction) -> str:
    if v.denominator == 1:
        return str(v.numerator)
    aprox = f"{float(v):.4g}".replace(".", ",")
    return f"{v.numerator}/{v.denominator} (aprox. {aprox})"
 
 
def _achar_conta(texto: str) -> Optional[tuple]:
    """Se a mensagem tiver UMA conta aritmética clara, retorna (expressão, valor Fraction)."""
    t = texto.lower()
 
    # datas (12/05/2024) e milhares (1.000) são ambíguos: não tenta calcular
    if re.search(r"\d\.\d{3}(?!\d)", t) or re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", t):
        return None
 
    # "25% de 80"
    m = re.search(r"(\d{1,9}(?:[.,]\d{1,6})?)\s*%\s*de\s*(\d{1,9}(?:[.,]\d{1,6})?)", t)
    if m:
        pct = Fraction(m.group(1).replace(",", "."))
        base = Fraction(m.group(2).replace(",", "."))
        return f"{m.group(1)}% de {m.group(2)}", pct * base / 100
 
    t = t.replace("×", "*").replace("÷", "/").replace("^", "**")
    t = re.sub(r"(?<=\d)\s*x\s*(?=[\d(])", "*", t)   # 3x7 -> 3*7 (mas 4x + 2 continua sendo incógnita)
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)            # 1,5 -> 1.5
 
    validos = []
    for trecho in re.findall(r"[\d+\-*/().\s]+", t):
        expr = trecho.strip().rstrip(".").strip()
        if not re.search(r"\d", expr) or len(expr) > _LIMITE_EXPR:
            continue
        try:
            arvore = ast.parse(expr, mode="eval")
            if not isinstance(arvore.body, ast.BinOp):
                continue
            valido = _avaliar(arvore)
        except (SyntaxError, ValueError, ZeroDivisionError, RecursionError):
            continue
        if abs(valido) <= _LIMITE_VALOR:
            validos.append((expr, valido))
 
    # mais de uma conta na mesma mensagem = ambíguo, deixa o LLM decidir
    return validos[0] if len(validos) == 1 else None
 
 
_RE_RESPOSTA = re.compile(r"^\s*=?\s*(-?\d+(?:[.,]\d+)?)(?:\s*/\s*(\d+))?\s*\.?\s*$")
 
 
def _valor_resposta(texto: str) -> Optional[Fraction]:
    """Se a mensagem for SÓ um número (ou fração), retorna o valor."""
    m = _RE_RESPOSTA.match(texto)
    if not m:
        return None
    try:
        v = Fraction(m.group(1).replace(",", "."))
        if m.group(2):
            v /= int(m.group(2))
        return v
    except (ValueError, ZeroDivisionError):
        return None
 
 
def _nota(expr: str, valor: Fraction) -> str:
    return (
        f"GABARITO (calculado por programa, confiável, não recalcule): {expr} = {_formatar(valor)}. "
        "Use só para conferir a resposta do aluno e siga as regras acima sobre quando revelar o resultado."
    )
 
 
def analisar_matematica(pergunta: str, msgs_aluno_recentes: list):
    """
    Retorna (resposta_pronta, nota_para_o_prompt).
 
    - resposta_pronta: o aluno mandou só um número e ele bate com o resultado da última
      conta que ele propôs -> "Correto!" sem gastar LLM e sem risco de erro.
    - nota_para_o_prompt: gabarito calculado por programa, para o LLM apenas conferir.
    Equações com incógnita (4x + 2 = 38) não são tratadas aqui: seguem para o LLM.
 
    msgs_aluno_recentes: mensagens anteriores do aluno, da mais antiga para a mais nova,
    sem incluir a mensagem atual.
    """
    resposta = _valor_resposta(pergunta)
 
    if resposta is not None:
        for msg in reversed(msgs_aluno_recentes):
            conta = _achar_conta(msg)
            if conta:
                expr, valor = conta
                if resposta == valor:
                    return RESPOSTA_CORRETO, None
                return None, _nota(expr, valor)
        # sem conta pendente: pode ser uma conta digitada sozinha (ex.: "10 / 4"), segue abaixo
 
    conta = _achar_conta(pergunta)
    if conta:
        return None, _nota(*conta)
 
    # sem conta nova ("não sei", "como faz?"): mantém o gabarito da conta pendente
    for msg in reversed(msgs_aluno_recentes):
        conta = _achar_conta(msg)
        if conta:
            return None, _nota(*conta)
    return None, None
 