
from .parser_asm import asmParser

# Create a single instance of the parser for reuse
parser = asmParser()

def parserASM(text, rule):
  try:
    ast = parser.parse(text, start=rule, whitespace='')
    return ast
  except:
    return None