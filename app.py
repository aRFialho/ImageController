import io
import os
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from PIL import Image
from psycopg import connect
from psycopg.rows import dict_row
from werkzeug.utils import secure_filename

app = Flask(__name__)

app.config["EXPORT_FOLDER"] = "exports"
app.config["SQLITE_DB_PATH"] = "gerenciador.db"
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")

MAX_URLS = 15
TARGET_MAX_KB = 345
COMPRESS_TRIGGER_KB = 350

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USING_POSTGRES = DATABASE_URL.lower().startswith("postgres://") or DATABASE_URL.lower().startswith("postgresql://")


def ensure_directories() -> None:
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["EXPORT_FOLDER"], exist_ok=True)
    os.makedirs("uploads", exist_ok=True)


def q(sql: str) -> str:
    if USING_POSTGRES:
        return sql.replace("?", "%s")
    return sql


def get_db():
    if USING_POSTGRES:
        return connect(DATABASE_URL, row_factory=dict_row)

    conn = sqlite3.connect(app.config["SQLITE_DB_PATH"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def exec_sql(cursor, sql: str, params=()):
    cursor.execute(q(sql), params)


def fetch_all(cursor, sql: str, params=()):
    exec_sql(cursor, sql, params)
    return cursor.fetchall()


def fetch_one(cursor, sql: str, params=()):
    exec_sql(cursor, sql, params)
    return cursor.fetchone()


def normalize_col(name: str) -> str:
    lowered = str(name).strip().lower()
    return unicodedata.normalize("NFKD", lowered).encode("ASCII", "ignore").decode("utf-8")


def read_table(path: str, ext: str) -> pd.DataFrame:
    if ext == "csv":
        try:
            return pd.read_csv(
                path,
                sep=None,
                engine="python",
                encoding="utf-8",
                dtype=str,
                keep_default_na=False,
                on_bad_lines="skip",
            )
        except UnicodeDecodeError:
            return pd.read_csv(
                path,
                sep=None,
                engine="python",
                encoding="latin1",
                dtype=str,
                keep_default_na=False,
                on_bad_lines="skip",
            )

    return pd.read_excel(path, dtype=str)


def find_col(df: pd.DataFrame, names: list[str]):
    for candidate in names:
        if candidate in df.columns:
            return candidate
    return None


def unique_tab_name(cursor, desired_name: str) -> str:
    desired = desired_name.strip() or "Importado"
    existing_rows = fetch_all(cursor, "SELECT nome FROM abas")
    existing_names = {row["nome"] for row in existing_rows}

    if desired not in existing_names:
        return desired

    i = 2
    while True:
        candidate = f"{desired} ({i})"
        if candidate not in existing_names:
            return candidate
        i += 1


def table_has_column(cursor, table_name: str, column_name: str) -> bool:
    if USING_POSTGRES:
        row = fetch_one(
            cursor,
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
            LIMIT 1
            """,
            (table_name, column_name),
        )
        return bool(row)

    rows = fetch_all(cursor, f"PRAGMA table_info({table_name})")
    return any(r["name"] == column_name for r in rows)


def init_db() -> None:
    ensure_directories()

    conn = get_db()
    c = conn.cursor()

    if USING_POSTGRES:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS abas (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                ordem INTEGER NOT NULL
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS produtos (
                id SERIAL PRIMARY KEY,
                aba_id INTEGER NOT NULL REFERENCES abas(id) ON DELETE CASCADE,
                referencia TEXT NOT NULL,
                nome TEXT NOT NULL DEFAULT ''
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS imagens (
                id SERIAL PRIMARY KEY,
                produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                ordem INTEGER NOT NULL
            )
            """
        )
    else:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS abas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                ordem INTEGER NOT NULL
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aba_id INTEGER NOT NULL,
                referencia TEXT NOT NULL,
                nome TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (aba_id) REFERENCES abas(id) ON DELETE CASCADE
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS imagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                produto_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                ordem INTEGER NOT NULL,
                FOREIGN KEY (produto_id) REFERENCES produtos(id) ON DELETE CASCADE
            )
            """
        )

    if not table_has_column(c, "produtos", "nome"):
        exec_sql(c, "ALTER TABLE produtos ADD COLUMN nome TEXT")

    exec_sql(c, "UPDATE produtos SET nome = referencia WHERE nome IS NULL OR nome = ''")

    total_tabs = fetch_one(c, "SELECT COUNT(*) AS total FROM abas")["total"]
    if total_tabs == 0:
        exec_sql(c, "INSERT INTO abas (nome, ordem) VALUES (?, ?)", ("Aba Principal", 0))

    conn.commit()
    conn.close()


def build_image_row(cursor, product_id: int):
    rows = fetch_all(
        cursor,
        """
        SELECT id, url, ordem
        FROM imagens
        WHERE produto_id = ?
        ORDER BY ordem
        """,
        (product_id,),
    )
    return [{"id": r["id"], "url": r["url"], "ordem": r["ordem"]} for r in rows]


def save_image_bytes(file_bytes: bytes, extension: str) -> str:
    safe_ext = extension.lower().replace(".", "")
    safe_ext = safe_ext if safe_ext in {"jpg", "jpeg", "png", "webp", "gif"} else "jpg"
    filename = f"img_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid4().hex}.{safe_ext}"
    full_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    with open(full_path, "wb") as f:
        f.write(file_bytes)

    return url_for("static", filename=f"uploads/{filename}")


def compress_to_target(file_bytes: bytes, target_kb: int):
    image = Image.open(io.BytesIO(file_bytes))

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")

    best_bytes = None
    best_size = 10**12

    def try_encode(img: Image.Image, quality: int):
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality, method=6)
        raw = out.getvalue()
        return raw, len(raw)

    for quality in [90, 84, 78, 72, 66, 60, 54, 48, 42, 36]:
        encoded, size = try_encode(image, quality)
        if size < best_size:
            best_bytes = encoded
            best_size = size
        if size <= target_kb * 1024:
            return encoded, "webp"

    resized = image
    for _ in range(10):
        w, h = resized.size
        if w < 500 or h < 500:
            break
        resized = resized.resize((int(w * 0.92), int(h * 0.92)), Image.Resampling.LANCZOS)

        for quality in [76, 68, 60, 52, 44, 36]:
            encoded, size = try_encode(resized, quality)
            if size < best_size:
                best_bytes = encoded
                best_size = size
            if size <= target_kb * 1024:
                return encoded, "webp"

    return best_bytes if best_bytes is not None else file_bytes, "webp"


init_db()


@app.route("/")
def index():
    conn = get_db()
    c = conn.cursor()

    abas_rows = fetch_all(c, "SELECT id, nome, ordem FROM abas ORDER BY ordem")

    if not abas_rows:
        exec_sql(c, "INSERT INTO abas (nome, ordem) VALUES (?, ?)", ("Aba Principal", 0))
        conn.commit()
        abas_rows = fetch_all(c, "SELECT id, nome, ordem FROM abas ORDER BY ordem")

    aba_ids = [a["id"] for a in abas_rows]

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search = request.args.get("search", "", type=str).strip()

    per_page = max(10, min(200, per_page))
    aba_atual = request.args.get("aba_id", type=int)

    if aba_atual not in aba_ids:
        aba_atual = aba_ids[0]

    offset = (page - 1) * per_page
    search_like = f"%{search}%"

    total_row = fetch_one(
        c,
        """
        SELECT COUNT(*) AS total
        FROM produtos
        WHERE aba_id = ?
          AND (
            LOWER(referencia) LIKE LOWER(?)
            OR LOWER(COALESCE(nome, '')) LIKE LOWER(?)
          )
        """,
        (aba_atual, search_like, search_like),
    )
    total_produtos = total_row["total"] if total_row else 0

    product_rows = fetch_all(
        c,
        """
        SELECT id, referencia, nome
        FROM produtos
        WHERE aba_id = ?
          AND (
            LOWER(referencia) LIKE LOWER(?)
            OR LOWER(COALESCE(nome, '')) LIKE LOWER(?)
          )
        ORDER BY id
        LIMIT ? OFFSET ?
        """,
        (aba_atual, search_like, search_like, per_page, offset),
    )

    produtos = []
    for p in product_rows:
        imgs = build_image_row(c, p["id"])
        produtos.append(
            {
                "id": p["id"],
                "nome": p["nome"] or p["referencia"],
                "referencia": p["referencia"],
                "urls": [img["url"] for img in imgs],
            }
        )

    conn.close()

    total_pages = max(1, (total_produtos + per_page - 1) // per_page)

    return render_template(
        "index.html",
        enumerated_abas=[(i, (a["id"], a["nome"])) for i, a in enumerate(abas_rows)],
        abas=[(a["id"], a["nome"]) for a in abas_rows],
        aba_atual=aba_atual,
        produtos={aba_atual: produtos},
        col_range=list(range(1, MAX_URLS + 1)),
        max_urls=MAX_URLS,
        pagination_data={
            "current_page": page,
            "total_pages": total_pages,
            "total_items": total_produtos,
            "per_page": per_page,
            "search_ref": search,
        },
    )


@app.route("/importar", methods=["POST"])
def importar():
    files = request.files.getlist("files")
    if not files and "file" in request.files:
        files = [request.files["file"]]

    valid_files = [f for f in files if f and f.filename]
    if not valid_files:
        return "Nenhum arquivo enviado", 400

    conn = get_db()
    c = conn.cursor()

    for file in valid_files:
        filename = secure_filename(file.filename)
        if not filename:
            continue

        ext = filename.lower().split(".")[-1]
        tmp_name = f"tmp_{uuid4().hex}_{filename}"
        path = os.path.join("uploads", tmp_name)
        file.save(path)

        try:
            df = read_table(path, ext)
            df.columns = [normalize_col(col) for col in df.columns]

            max_row = fetch_one(c, "SELECT MAX(ordem) AS max_ord FROM abas")
            max_ord = (max_row["max_ord"] or 0) if max_row else 0

            base_tab_name = Path(filename).stem.strip() or f"Importado {datetime.now().strftime('%d-%m %H-%M')}"
            tab_name = unique_tab_name(c, base_tab_name)

            exec_sql(c, "INSERT INTO abas (nome, ordem) VALUES (?, ?)", (tab_name, max_ord + 1))
            aba_id = c.lastrowid if not USING_POSTGRES else fetch_one(c, "SELECT currval(pg_get_serial_sequence('abas','id')) AS id")["id"]

            col_nome = find_col(df, ["nome produto", "nome"])
            col_variacao = find_col(df, ["valor variacao principal"])
            col_ref = find_col(df, ["referencia"])

            col_img1 = find_col(df, ["imagem principal", "imagem", "imagem 1", "url 1"])
            if col_img1 == "imagem":
                col_img1 = None

            col_img2 = find_col(df, ["imagem 2", "url 2"])
            col_img3 = find_col(df, ["imagem 3", "url 3"])
            col_img4 = find_col(df, ["imagem 4", "url 4"])
            col_adic = find_col(df, ["imagens adicionais", "imagem adicionais", "imagens adicionais url"])

            if not col_ref:
                continue

            for _, row in df.iterrows():
                ref = str(row[col_ref]).strip() if col_ref and row[col_ref] else ""
                if not ref:
                    continue

                nome = str(row[col_nome]).strip() if col_nome and row[col_nome] else ref
                if col_variacao and str(row[col_variacao]).strip():
                    nome = f"{nome} {str(row[col_variacao]).strip()}"

                exec_sql(
                    c,
                    """
                    INSERT INTO produtos (aba_id, referencia, nome)
                    VALUES (?, ?, ?)
                    """,
                    (aba_id, ref, nome),
                )

                pid = c.lastrowid if not USING_POSTGRES else fetch_one(c, "SELECT currval(pg_get_serial_sequence('produtos','id')) AS id")["id"]

                raw_urls = []
                if col_img1 and str(row[col_img1]).strip():
                    raw_urls.append(str(row[col_img1]).strip())

                for col in [col_img2, col_img3, col_img4]:
                    if col and str(row[col]).strip():
                        raw_urls.append(str(row[col]).strip())

                if col_adic and str(row[col_adic]).strip():
                    extras = str(row[col_adic]).split(",")
                    for link in extras:
                        link = link.strip()
                        if link:
                            raw_urls.append(link)

                urls = []
                for link in raw_urls:
                    link_low = link.lower()
                    if "youtube." in link_low or "youtu.be" in link_low:
                        continue
                    urls.append(link)

                for ordem, url in enumerate(urls[:MAX_URLS], start=1):
                    exec_sql(
                        c,
                        """
                        INSERT INTO imagens (produto_id, url, ordem)
                        VALUES (?, ?, ?)
                        """,
                        (pid, url, ordem),
                    )
        finally:
            if os.path.exists(path):
                os.remove(path)

    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.route("/exportar", methods=["POST"])
def exportar():
    aba_id = request.form.get("aba_id", type=int)

    conn = get_db()
    c = conn.cursor()

    produtos = fetch_all(
        c,
        """
        SELECT id, referencia, nome
        FROM produtos
        WHERE aba_id = ?
        ORDER BY id
        """,
        (aba_id,),
    )

    data = []
    for p in produtos:
        img_rows = fetch_all(
            c,
            """
            SELECT url
            FROM imagens
            WHERE produto_id = ?
            ORDER BY ordem
            """,
            (p["id"],),
        )
        urls = [r["url"] for r in img_rows]
        urls += [""] * (MAX_URLS - len(urls))

        row = {"Nome": p["nome"], "Referencia": p["referencia"]}
        for i in range(MAX_URLS):
            row[f"URL {i + 1}"] = urls[i]

        data.append(row)

    conn.close()

    df = pd.DataFrame(data)
    out = io.BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)

    return Response(
        out,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=aba_{aba_id}.xlsx"},
    )


@app.route("/adicionar_url", methods=["POST"])
def adicionar_url():
    payload = request.get_json(force=True)
    pid = int(payload["produto_id"])
    url = str(payload["url"]).strip()

    if not url:
        return jsonify(error=True, message="URL vazia"), 400

    conn = get_db()
    c = conn.cursor()

    max_row = fetch_one(c, "SELECT MAX(ordem) AS max_ord FROM imagens WHERE produto_id = ?", (pid,))
    ordem = (max_row["max_ord"] or 0) + 1

    if ordem > MAX_URLS:
        conn.close()
        return jsonify(error=True, message=f"Maximo de {MAX_URLS} imagens por produto"), 400

    exec_sql(
        c,
        """
        INSERT INTO imagens (produto_id, url, ordem)
        VALUES (?, ?, ?)
        """,
        (pid, url, ordem),
    )

    conn.commit()
    conn.close()
    return jsonify(success=True, ordem=ordem)


@app.route("/excluir_imagem", methods=["POST"])
def excluir_imagem():
    pid = int(request.json["produto_id"])
    col = int(request.json["col"])

    conn = get_db()
    c = conn.cursor()

    row = fetch_one(
        c,
        """
        SELECT id
        FROM imagens
        WHERE produto_id = ?
        ORDER BY ordem
        LIMIT 1 OFFSET ?
        """,
        (pid, col - 1),
    )

    if not row:
        conn.close()
        return jsonify(error=True), 400

    exec_sql(c, "DELETE FROM imagens WHERE id = ?", (row["id"],))

    imgs = fetch_all(c, "SELECT id FROM imagens WHERE produto_id = ? ORDER BY ordem", (pid,))
    for i, img in enumerate(imgs, start=1):
        exec_sql(c, "UPDATE imagens SET ordem = ? WHERE id = ?", (i, img["id"]))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/reordenar", methods=["POST"])
def reordenar():
    pid = int(request.json["produto_id"])
    urls = request.json["urls"]

    conn = get_db()
    c = conn.cursor()

    exec_sql(c, "DELETE FROM imagens WHERE produto_id = ?", (pid,))

    for ordem, url in enumerate(urls[:MAX_URLS], start=1):
        exec_sql(c, "INSERT INTO imagens (produto_id, url, ordem) VALUES (?, ?, ?)", (pid, url, ordem))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/deletar_produto", methods=["POST"])
def deletar_produto():
    pid = int(request.json["produto_id"])

    conn = get_db()
    c = conn.cursor()

    exec_sql(c, "DELETE FROM produtos WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

    return jsonify(success=True)


@app.route("/deletar_produtos_selecionados", methods=["POST"])
def deletar_produtos_selecionados():
    ids = request.json["prod_ids"]

    conn = get_db()
    c = conn.cursor()

    for pid in ids:
        exec_sql(c, "DELETE FROM produtos WHERE id = ?", (int(pid),))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/mover_imagens", methods=["POST"])
def mover_imagens():
    origem = int(request.json["origem"])
    destino = int(request.json["destino"])

    conn = get_db()
    c = conn.cursor()

    urls_rows = fetch_all(c, "SELECT url FROM imagens WHERE produto_id = ? ORDER BY ordem", (origem,))
    urls = [x["url"] for x in urls_rows]

    exec_sql(c, "DELETE FROM imagens WHERE produto_id = ?", (origem,))

    base_row = fetch_one(c, "SELECT MAX(ordem) AS max_ord FROM imagens WHERE produto_id = ?", (destino,))
    base = base_row["max_ord"] or 0

    ordem = base + 1
    for url in urls:
        if ordem > MAX_URLS:
            break
        exec_sql(c, "INSERT INTO imagens (produto_id, url, ordem) VALUES (?, ?, ?)", (destino, url, ordem))
        ordem += 1

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/duplicar_produto", methods=["POST"])
def duplicar_produto():
    pid = int(request.json["produto_id"])

    conn = get_db()
    c = conn.cursor()

    p = fetch_one(c, "SELECT * FROM produtos WHERE id = ?", (pid,))
    if not p:
        conn.close()
        return jsonify(error=True), 404

    nova_ref = p["referencia"] + " (copy)"
    novo_nome = (p["nome"] or p["referencia"]) + " (copy)"

    exec_sql(
        c,
        "INSERT INTO produtos (aba_id, referencia, nome) VALUES (?, ?, ?)",
        (p["aba_id"], nova_ref, novo_nome),
    )

    new_id = c.lastrowid if not USING_POSTGRES else fetch_one(c, "SELECT currval(pg_get_serial_sequence('produtos','id')) AS id")["id"]

    imgs = fetch_all(c, "SELECT url, ordem FROM imagens WHERE produto_id = ? ORDER BY ordem", (pid,))
    for img in imgs:
        exec_sql(c, "INSERT INTO imagens (produto_id, url, ordem) VALUES (?, ?, ?)", (new_id, img["url"], img["ordem"]))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/renomear_aba", methods=["POST"])
def renomear_aba():
    aba_id = int(request.json["aba_id"])
    nome = str(request.json["nome"]).strip()

    if not nome:
        return jsonify(error=True, message="Nome vazio"), 400

    conn = get_db()
    c = conn.cursor()

    exec_sql(c, "UPDATE abas SET nome = ? WHERE id = ?", (nome, aba_id))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/excluir_aba", methods=["POST"])
def excluir_aba():
    aba_id = int(request.json["aba_id"])

    conn = get_db()
    c = conn.cursor()

    product_rows = fetch_all(c, "SELECT id FROM produtos WHERE aba_id = ?", (aba_id,))
    for p in product_rows:
        exec_sql(c, "DELETE FROM imagens WHERE produto_id = ?", (p["id"],))

    exec_sql(c, "DELETE FROM produtos WHERE aba_id = ?", (aba_id,))
    exec_sql(c, "DELETE FROM abas WHERE id = ?", (aba_id,))

    count_row = fetch_one(c, "SELECT COUNT(*) AS total FROM abas")
    if count_row and count_row["total"] == 0:
        exec_sql(c, "INSERT INTO abas (nome, ordem) VALUES (?, ?)", ("Aba Principal", 0))

    next_tab_row = fetch_one(c, "SELECT id FROM abas ORDER BY ordem LIMIT 1")
    next_tab_id = next_tab_row["id"] if next_tab_row else None

    conn.commit()
    conn.close()

    return jsonify(success=True, next_aba_id=next_tab_id)


@app.route("/duplicar_aba", methods=["POST"])
def duplicar_aba():
    aba_id = int(request.json["aba_id"])

    conn = get_db()
    c = conn.cursor()

    aba = fetch_one(c, "SELECT nome FROM abas WHERE id = ?", (aba_id,))
    if not aba:
        conn.close()
        return jsonify(error=True), 404

    max_row = fetch_one(c, "SELECT MAX(ordem) AS max_ord FROM abas")
    max_ord = max_row["max_ord"] or 0

    novo_nome = unique_tab_name(c, f"{aba['nome']} (copy)")
    exec_sql(c, "INSERT INTO abas (nome, ordem) VALUES (?, ?)", (novo_nome, max_ord + 1))

    nova_id = c.lastrowid if not USING_POSTGRES else fetch_one(c, "SELECT currval(pg_get_serial_sequence('abas','id')) AS id")["id"]

    produtos = fetch_all(c, "SELECT id, referencia, nome FROM produtos WHERE aba_id = ?", (aba_id,))

    for p in produtos:
        exec_sql(c, "INSERT INTO produtos (aba_id, referencia, nome) VALUES (?, ?, ?)", (nova_id, p["referencia"], p["nome"]))
        new_pid = c.lastrowid if not USING_POSTGRES else fetch_one(c, "SELECT currval(pg_get_serial_sequence('produtos','id')) AS id")["id"]

        imgs = fetch_all(c, "SELECT url, ordem FROM imagens WHERE produto_id = ? ORDER BY ordem", (p["id"],))
        for img in imgs:
            exec_sql(c, "INSERT INTO imagens (produto_id, url, ordem) VALUES (?, ?, ?)", (new_pid, img["url"], img["ordem"]))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/mover_aba_cima", methods=["POST"])
def mover_aba_cima():
    aba_id = int(request.json["aba_id"])

    conn = get_db()
    c = conn.cursor()

    atual = fetch_one(c, "SELECT id, ordem FROM abas WHERE id = ?", (aba_id,))
    if not atual:
        conn.close()
        return jsonify(error=True), 404

    acima = fetch_one(
        c,
        """
        SELECT id, ordem
        FROM abas
        WHERE ordem < ?
        ORDER BY ordem DESC
        LIMIT 1
        """,
        (atual["ordem"],),
    )

    if acima:
        exec_sql(c, "UPDATE abas SET ordem = ? WHERE id = ?", (atual["ordem"], acima["id"]))
        exec_sql(c, "UPDATE abas SET ordem = ? WHERE id = ?", (acima["ordem"], aba_id))

    conn.commit()
    conn.close()

    return jsonify(success=True)


@app.route("/mover_aba_baixo", methods=["POST"])
def mover_aba_baixo():
    aba_id = int(request.json["aba_id"])

    conn = get_db()
    c = conn.cursor()

    atual = fetch_one(c, "SELECT id, ordem FROM abas WHERE id = ?", (aba_id,))
    if not atual:
        conn.close()
        return jsonify(error=True), 404

    abaixo = fetch_one(
        c,
        """
        SELECT id, ordem
        FROM abas
        WHERE ordem > ?
        ORDER BY ordem ASC
        LIMIT 1
        """,
        (atual["ordem"],),
    )

    if abaixo:
        exec_sql(c, "UPDATE abas SET ordem = ? WHERE id = ?", (atual["ordem"], abaixo["id"]))
        exec_sql(c, "UPDATE abas SET ordem = ? WHERE id = ?", (abaixo["ordem"], aba_id))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/reordenar_abas", methods=["POST"])
def reordenar_abas():
    ordem = request.json["ordem"]

    conn = get_db()
    c = conn.cursor()

    for i, aba_id in enumerate(ordem):
        exec_sql(c, "UPDATE abas SET ordem = ? WHERE id = ?", (i, int(aba_id)))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/mover_para_coluna", methods=["POST"])
def mover_para_coluna():
    coluna = int(request.json["coluna"])
    data = request.json["data"]

    conn = get_db()
    c = conn.cursor()

    for produto_id, urls_sel in data.items():
        pid = int(produto_id)

        atual_rows = fetch_all(c, "SELECT url FROM imagens WHERE produto_id = ? ORDER BY ordem", (pid,))
        atual = [u["url"] for u in atual_rows]

        atual = [u for u in atual if u not in urls_sel]

        pos = max(0, min(len(atual), coluna - 1))
        nova = (atual[:pos] + urls_sel + atual[pos:])[:MAX_URLS]

        exec_sql(c, "DELETE FROM imagens WHERE produto_id = ?", (pid,))

        for ordem, url in enumerate(nova, start=1):
            exec_sql(c, "INSERT INTO imagens (produto_id, url, ordem) VALUES (?, ?, ?)", (pid, url, ordem))

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/colar_imagens", methods=["POST"])
def colar_imagens():
    data = request.get_json(force=True)
    prod_ids = data.get("prod_ids", [])
    urls = data.get("urls", [])

    if not prod_ids or not urls:
        return jsonify(error="Nenhum produto ou url enviada"), 400

    conn = get_db()
    c = conn.cursor()

    for pid in prod_ids:
        pid = int(pid)

        atual_rows = fetch_all(c, "SELECT url FROM imagens WHERE produto_id = ? ORDER BY ordem", (pid,))
        atual = [r["url"] for r in atual_rows]

        vagas = MAX_URLS - len(atual)
        if vagas <= 0:
            continue

        to_add = [u for u in urls if u not in atual][:vagas]

        ordem = len(atual) + 1
        for u in to_add:
            exec_sql(c, "INSERT INTO imagens (produto_id, url, ordem) VALUES (?, ?, ?)", (pid, u, ordem))
            ordem += 1

    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/upload_imagem", methods=["POST"])
def upload_imagem():
    if "file" not in request.files:
        return jsonify(error="Nenhum arquivo enviado"), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="Arquivo vazio"), 400

    original_bytes = file.read()
    if not original_bytes:
        return jsonify(error="Arquivo invalido"), 400

    original_kb = len(original_bytes) / 1024
    final_bytes = original_bytes
    ext = Path(file.filename).suffix.replace(".", "") or "jpg"
    compressed = False

    if original_kb > COMPRESS_TRIGGER_KB:
        final_bytes, ext = compress_to_target(original_bytes, TARGET_MAX_KB)
        compressed = True

    final_kb = len(final_bytes) / 1024
    image_url = save_image_bytes(final_bytes, ext)

    message = None
    if compressed:
        message = f"Imagem compactada de {original_kb:.1f}KB para {final_kb:.1f}KB"

    return jsonify(
        url=image_url,
        compressed=compressed,
        original_kb=round(original_kb, 1),
        final_kb=round(final_kb, 1),
        message=message,
    )


@app.route("/atualizar_nome", methods=["POST"])
def atualizar_nome():
    data = request.get_json(force=True)
    pid = int(data["produto_id"])
    nome = str(data["nome"]).strip()

    if not nome:
        return jsonify(success=False, message="Nome vazio"), 400

    conn = get_db()
    c = conn.cursor()

    exec_sql(c, "UPDATE produtos SET nome = ? WHERE id = ?", (nome, pid))

    conn.commit()
    conn.close()

    return jsonify(success=True)


@app.route("/health")
def health():
    return jsonify(status="ok", database="postgres" if USING_POSTGRES else "sqlite")


if __name__ == "__main__":
    app.run(debug=True)
