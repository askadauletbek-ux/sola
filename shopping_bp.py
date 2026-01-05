# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
from flask import Blueprint, request, jsonify, session, render_template
from sqlalchemy import text, inspect as sa_inspect
from sqlalchemy.exc import ProgrammingError, OperationalError, SQLAlchemyError
from openai import OpenAI

from extensions import db
from models import User, Diet

shopping_bp = Blueprint("shopping_bp", __name__)


# ---------------- OpenAI ----------------
def _get_openai_client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    return OpenAI(api_key=key)


# ---------------- helpers ----------------
def _current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


def _json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _is_pg() -> bool:
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


# ---------------- schema bootstrap (robust) ----------------
_SCHEMA_READY = False


def _create_schema():
    """Безусловная попытка создать таблицы (идемпотентно)."""
    pg = _is_pg()

    ddl_lists = f"""
    CREATE TABLE IF NOT EXISTS shopping_lists (
        id          {'BIGSERIAL' if pg else 'INTEGER'} PRIMARY KEY,
        user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        diet_id     BIGINT NOT NULL REFERENCES diets(id) ON DELETE CASCADE,
        status      VARCHAR(20) NOT NULL DEFAULT 'ready',
        created_at  {'TIMESTAMPTZ NOT NULL' if pg else 'TIMESTAMP NOT NULL'} DEFAULT {'NOW()' if pg else '(CURRENT_TIMESTAMP)'}
    );
    """
    ddl_lists_idx = """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_shopping_lists_user_diet
      ON shopping_lists(user_id, diet_id);
    """
    ddl_items = f"""
    CREATE TABLE IF NOT EXISTS shopping_items (
        id          {'BIGSERIAL' if pg else 'INTEGER'} PRIMARY KEY,
        list_id     BIGINT NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
        meal_type   VARCHAR(32),
        item_name   TEXT NOT NULL,
        qty         {'NUMERIC' if pg else 'REAL'},
        unit        TEXT,
        kaspi_query TEXT,
        kaspi_url   TEXT,
        price       {'NUMERIC' if pg else 'REAL'},
        meta        {'JSONB' if pg else 'TEXT'},
        created_at  {'TIMESTAMPTZ NOT NULL' if pg else 'TIMESTAMP NOT NULL'} DEFAULT {'NOW()' if pg else '(CURRENT_TIMESTAMP)'}
    );
    """

    with db.engine.begin() as con:
        con.execute(text(ddl_lists))
        con.execute(text(ddl_lists_idx))
        con.execute(text(ddl_items))


def _ensure_schema_once(force: bool = False):
    """Пытаемся создать схему один раз за жизнь процесса.
    Если users/diets ещё нет — FK упадёт -> отложим до следующего запроса.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    try:
        _create_schema()
        # проверим наличие наших таблиц
        insp = sa_inspect(db.engine)
        if insp.has_table("shopping_lists") and insp.has_table("shopping_items"):
            _SCHEMA_READY = True
    except (ProgrammingError, OperationalError) as e:
        _SCHEMA_READY = False
    except SQLAlchemyError:
        _SCHEMA_READY = False


@shopping_bp.record_once
def _on_register(state):
    app = state.app
    try:
        with app.app_context():
            _ensure_schema_once()
    except Exception as e:
        try:
            app.logger.exception("shopping_bp init failed: %s", e)
        except Exception:
            print(f"[shopping_bp] init failed: {e}")


@shopping_bp.before_app_request
def _lazy_bootstrap_bp():
    if not _SCHEMA_READY:
        _ensure_schema_once()


# ---------------- DAO ----------------
def _get_or_create_list_id(user_id: int, diet_id: int) -> int:
    _ensure_schema_once()
    with db.engine.begin() as con:
        row = con.execute(
            text("SELECT id FROM shopping_lists WHERE user_id=:u AND diet_id=:d"),
            {"u": user_id, "d": diet_id}
        ).mappings().first()
        if row:
            return int(row["id"])
        row = con.execute(
            text(f"""
                INSERT INTO shopping_lists (user_id, diet_id, status, created_at)
                VALUES (:u, :d, 'ready', {'NOW()' if _is_pg() else '(CURRENT_TIMESTAMP)'})
                RETURNING id
            """),
            {"u": user_id, "d": diet_id}
        ).mappings().first()
        return int(row["id"])


def _replace_items(list_id: int, items: list[dict]) -> None:
    _ensure_schema_once()
    pg = _is_pg()
    with db.engine.begin() as con:
        con.execute(text("DELETE FROM shopping_items WHERE list_id=:lid"), {"lid": list_id})
        if not items:
            return
        ins = text(f"""
            INSERT INTO shopping_items
            (list_id, meal_type, item_name, qty, unit, kaspi_query, kaspi_url, price, meta, created_at)
            VALUES
            (:list_id, :meal_type, :item_name, :qty, :unit, :kaspi_query, :kaspi_url, :price,
             {'CAST(:meta AS JSONB)' if pg else ':meta'},
             {'NOW()' if pg else 'CURRENT_TIMESTAMP'})
        """)
        for it in items:
            meal_type = (it.get("meal_type") or "").lower() or None
            product_name = it.get("product_name") or it.get("item_name") or it.get("name")
            total_grams = it.get("total_grams") or it.get("qty")
            unit = it.get("unit") or "g"
            kaspi_query = it.get("kaspi_query")
            kaspi_url = it.get("kaspi_url")
            price = it.get("price")
            meta = it.get("meta") or {}
            meta = {
                **meta,
                "pack_grams": it.get("pack_grams"),
                "quantity_packs": it.get("quantity_packs"),
            }
            con.execute(ins, {
                "list_id": list_id,
                "meal_type": meal_type,
                "item_name": product_name,
                "qty": total_grams,
                "unit": unit,
                "kaspi_query": kaspi_query,
                "kaspi_url": kaspi_url,
                "price": price,
                "meta": _json_dumps(meta),
            })


def _fetch_items(list_id: int) -> list[dict]:
    _ensure_schema_once()
    with db.engine.begin() as con:
        rows = con.execute(
            text("""
                SELECT id, meal_type, item_name, qty, unit, kaspi_query, kaspi_url, price, meta
                FROM shopping_items
                WHERE list_id=:lid
                ORDER BY meal_type, id ASC
            """),
            {"lid": list_id}
        ).mappings().all()
    out = []
    for r in rows:
        meta = r["meta"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        out.append({
            "id": r["id"],
            "meal_type": (r["meal_type"] or "").lower(),
            "product_name": r["item_name"],
            "total_grams": float(r["qty"]) if r["qty"] is not None else None,
            "unit": r["unit"],
            "kaspi_query": r["kaspi_query"],
            "kaspi_url": r["kaspi_url"],
            "price": float(r["price"]) if r["price"] is not None else None,
            "pack_grams": (meta or {}).get("pack_grams"),
            "quantity_packs": (meta or {}).get("quantity_packs") or 1,
            "meta": meta or {}
        })
    return out


def _diet_meals_payload(d: Diet) -> dict:
    payload = {}
    for k in ("breakfast", "lunch", "dinner", "snack"):
        val = getattr(d, k, "[]") or "[]"
        try:
            payload[k] = json.loads(val)
        except Exception:
            payload[k] = []
    return payload


def _group_for_front(items: list[dict]) -> dict:
    out = {k: [] for k in ("breakfast", "lunch", "dinner", "snack", "other")}
    for it in items:
        mt = (it.get("meal_type") or "").lower() or "other"
        if mt not in out:
            mt = "other"
        out[mt].append({
            "product_name": it.get("product_name"),
            "total_grams": it.get("total_grams"),
            "pack_grams": it.get("pack_grams"),
            "quantity_packs": it.get("quantity_packs") or 1,
            "kaspi_query": it.get("kaspi_query"),
            "kaspi_url": it.get("kaspi_url"),
            "price": it.get("price"),
            "unit": it.get("unit") or 'шт',
        })
    return out


# ---------------- Routes ----------------
@shopping_bp.route("/cart/<int:diet_id>")
def shopping_cart_page(diet_id):
    u = _current_user()
    if not u:
        return "unauthorized", 401

    diet = Diet.query.filter_by(id=diet_id, user_id=u.id).first()
    if not diet:
        return "Диета не найдена", 404

    return render_template("shopping_cart.html", diet=diet)


@shopping_bp.get("/list")
def shopping_list_get():
    _ensure_schema_once()
    u = _current_user()
    if not u:
        return jsonify({"ok": False, "message": "unauthorized"}), 401

    diet_id = request.args.get("diet_id", type=int)
    if not diet_id:
        return jsonify({"ok": False, "message": "diet_id is required"}), 400

    diet = Diet.query.filter_by(id=diet_id, user_id=u.id).first()
    if not diet:
        return jsonify({"ok": False, "message": "Диета не найдена"}), 404

    try:
        list_id = _get_or_create_list_id(u.id, diet_id)
        items = _fetch_items(list_id)
        return jsonify({
            "ok": True,
            "list_id": list_id,
            "items_by_meal": _group_for_front(items)
        })
    except SQLAlchemyError as e:
        return jsonify({"ok": False, "message": f"db error: {e}"}), 500


@shopping_bp.post("/reset")
def shopping_list_reset():
    u = _current_user()
    if not u:
        return jsonify({"ok": False, "message": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    diet_id = int(data.get("diet_id") or 0)
    if not diet_id:
        return jsonify({"ok": False, "message": "diet_id is required"}), 400

    with db.engine.begin() as con:
        row = con.execute(text("SELECT id FROM shopping_lists WHERE user_id=:u AND diet_id=:d"),
                          {"u": u.id, "d": diet_id}).mappings().first()
        if row:
            con.execute(text("DELETE FROM shopping_items WHERE list_id=:lid"), {"lid": row["id"]})
            # Оставляем сам список, чтобы не создавать его заново
    return jsonify({"ok": True})


@shopping_bp.post("/build")
def shopping_build():
    _ensure_schema_once()
    u = _current_user()
    if not u:
        return jsonify({"ok": False, "message": "unauthorized"}), 401

    client = _get_openai_client()
    if client is None:
        return jsonify({"ok": False, "message": "OPENAI_API_KEY не задан. Укажи ключ в .env"}), 500

    data = request.get_json(silent=True) or {}
    diet_id = int(data.get("diet_id") or 0)
    if not diet_id:
        return jsonify({"ok": False, "message": "diet_id is required"}), 400

    diet = Diet.query.filter_by(id=diet_id, user_id=u.id).first()
    if not diet:
        return jsonify({"ok": False, "message": "Диета не найдена"}), 404

    meals = _diet_meals_payload(diet)

    try:
        comp = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты помощник по формированию списка покупок из суточной диеты.\n"
                        "На входе блюда с граммовкой. Собери единицы закупки.\n"
                        "Верни JSON строго с ключом 'items' (массив объектов), каждый объект со схемой:\n"
                        "{meal_type:'breakfast|lunch|dinner|snack', "
                        " product_name:'строка', total_grams:число, pack_grams:число|null, "
                        " quantity_packs:целое>=1, kaspi_query:'строка', kaspi_url:'строка|null', price:число|null, meta:{}}\n"
                        "total_grams — сколько всего граммов нужно по рецептам для позиции на день.\n"
                        "Если уверен в конкретном товаре на kaspi.kz — укажи kaspi_url; если нет — оставь null и дай точный kaspi_query.\n"
                        "Если не можешь оценить упаковку — pack_grams можно null, quantity_packs=1 по умолчанию."
                    )
                },
                {
                    "role": "user",
                    "content": _json_dumps({
                        "diet_id": diet.id,
                        "meals": meals
                    })
                }
            ]
        )
        raw = (comp.choices[0].message.content or "{}").strip()
        parsed = json.loads(raw)
        items = list(parsed.get("items") or [])
    except Exception as e:
        return jsonify({"ok": False, "message": f"OpenAI error: {e}"}), 500

    for it in items:
        if (it.get("kaspi_query") or it.get("product_name")) and not it.get("kaspi_url"):
            it["kaspi_url"] = None

    try:
        list_id = _get_or_create_list_id(u.id, diet.id)
        _replace_items(list_id, items)
        grouped = _group_for_front(_fetch_items(list_id))
        return jsonify({"ok": True, "list_id": list_id, "items_by_meal": grouped})
    except SQLAlchemyError as e:
        return jsonify({"ok": False, "message": f"db error: {e}"}), 500