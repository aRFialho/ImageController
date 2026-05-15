import sqlite3

print("Conectando ao banco...")
conn = sqlite3.connect("gerenciador.db")
c = conn.cursor()

# 1) Tentar adicionar a coluna nome
try:
    c.execute("ALTER TABLE produtos ADD COLUMN nome TEXT")
    print("Coluna 'nome' criada com sucesso!")
except Exception as e:
    print("Aviso:", e)

# 2) Preencher nomes antigos = referência
c.execute("""
    UPDATE produtos 
    SET nome = referencia 
    WHERE nome IS NULL OR nome = ''
""")

conn.commit()
conn.close()
print("Migração concluída com sucesso!")