"""Comptabilite des jetons consommes par les appels aux modeles."""
from datetime import date, timedelta

from models import get_db


def record(provider, model, purpose, input_tokens, output_tokens, cached_tokens, cost_usd):
    """Enregistre la consommation d'un appel."""
    db = get_db()
    db.execute(
        "INSERT INTO usage (provider, model, purpose, input_tokens, output_tokens,"
        "  cached_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (provider, model, purpose, input_tokens, output_tokens, cached_tokens, cost_usd),
    )
    db.commit()


def summary(days=30):
    """Retourne la consommation agregee sur une periode glissante.

    :return: {total, par_jour, par_fournisseur, moyenne_par_appel}
    """
    depuis = (date.today() - timedelta(days=days - 1)).isoformat()

    total = dict(
        get_db().execute(
            "SELECT COUNT(*) AS calls,"
            "  COALESCE(SUM(input_tokens), 0)  AS input_tokens,"
            "  COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            "  COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            "  COALESCE(SUM(cost_usd), 0)      AS cost_usd"
            " FROM usage WHERE date(created_at) >= ?",
            (depuis,),
        ).fetchone()
    )

    total["avg_tokens"] = (
        round((total["input_tokens"] + total["output_tokens"]) / total["calls"])
        if total["calls"]
        else 0
    )

    par_jour = [
        dict(row)
        for row in get_db().execute(
            "SELECT date(created_at) AS day, COUNT(*) AS calls,"
            "  SUM(input_tokens + output_tokens) AS tokens,"
            "  SUM(cost_usd) AS cost_usd"
            " FROM usage WHERE date(created_at) >= ?"
            " GROUP BY day ORDER BY day",
            (depuis,),
        ).fetchall()
    ]

    par_fournisseur = [
        dict(row)
        for row in get_db().execute(
            "SELECT provider, model, COUNT(*) AS calls,"
            "  SUM(input_tokens) AS input_tokens,"
            "  SUM(output_tokens) AS output_tokens,"
            "  SUM(cached_tokens) AS cached_tokens,"
            "  SUM(cost_usd) AS cost_usd"
            " FROM usage WHERE date(created_at) >= ?"
            " GROUP BY provider, model ORDER BY calls DESC",
            (depuis,),
        ).fetchall()
    ]

    return {
        "days": days,
        "total": total,
        "by_day": par_jour,
        "by_provider": par_fournisseur,
    }


def purge(days_kept=180):
    """Supprime les releves plus anciens que la periode conservee."""
    limite = (date.today() - timedelta(days=days_kept)).isoformat()
    db = get_db()
    cursor = db.execute("DELETE FROM usage WHERE date(created_at) < ?", (limite,))
    db.commit()
    return cursor.rowcount
