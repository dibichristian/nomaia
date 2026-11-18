"""Source unique du logo NOMAIA : le mot « nomaia » en lettres geometriques.

Chaque lettre peut porter une fonction de l'application :
  o : anneau d'avancement (progression du jour)
  a : cadran d'horloge (plan du jour, agenda)       -- version complete
  i : point cyan (l'assistant qui signale quelque chose)
  a : coche (tache faite)                           -- version complete

Le script ecrit :
  docs/logo/nomaia-{complet,sobre}-{sombre,clair}.svg   logos fixes
  docs/logo/nomaia-anime-{sombre,clair}.svg             logo anime (README)
  templates/_logo.html                                  macro Jinja de l'interface

A relancer apres toute retouche de la geometrie :
    python tools/logo.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

W = 19                                  # epaisseur du trait
TOP, BASE = 70 + W / 2, 170 - W / 2     # axe du trait : hauteur d'x et ligne de base
CY, R = 120, 50 - W / 2                 # centre et rayon des panses (o, a)
GAP = 15                                # approche entre deux lettres
DETAIL_W = 7                            # trait des details (horloge, coche)
DOT_R = 14                              # rayon du point du i
RING_PCT = 72                           # remplissage de l'anneau sur les logos fixes

INDIGO, CYAN = "#6366f1", "#22d3ee"
THEMES = {
    "sombre": {"ink": "#e2e8f0", "track": "#334155"},
    "clair": {"ink": "#0f172a", "track": "#e2e8f0"},
}


def _n(value):
    """Nombre court pour le SVG."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def geometry(full):
    """Decrit le logo en elements abstraits.

    :param full: version complete (horloge et coche dans les « a »)
    :return: (elements, largeur) ; chaque element est un dict avec 'role'
        parmi ink, track, ring, detail, dot, et 'kind' path ou circle
    """
    items = []
    x = 20

    def path(d, role="ink", width=W, part=""):
        items.append({"kind": "path", "d": d, "role": role, "width": width, "part": part})

    def circle(cx, cy, r, role, part=""):
        items.append({"kind": "circle", "cx": cx, "cy": cy, "r": r, "role": role, "part": part})

    # n
    s = x + W / 2
    path(f"M{_n(s)} {_n(BASE)} V{_n(TOP)}", part="n")
    path(f"M{_n(s)} {_n(CY + 5)} A42 42 0 0 1 {_n(s + 84)} {_n(CY + 5)} V{_n(BASE)}", part="n")
    x = s + 84 + W / 2 + GAP

    # o : anneau d'avancement
    cx = x + R + W / 2
    circle(cx, CY, R, "track", part="o")
    circle(cx, CY, R, "ring", part="o")
    x = cx + R + W / 2 + GAP

    # m
    s = x + W / 2
    path(f"M{_n(s)} {_n(BASE)} V{_n(TOP)}", part="m")
    path(f"M{_n(s)} {_n(CY)} A35 35 0 0 1 {_n(s + 70)} {_n(CY)} V{_n(BASE)}", part="m")
    path(f"M{_n(s + 70)} {_n(CY)} A35 35 0 0 1 {_n(s + 140)} {_n(CY)} V{_n(BASE)}", part="m")
    x = s + 140 + W / 2 + GAP

    # a : cadran d'horloge
    cx = x + R + W / 2
    circle(cx, CY, R, "ink", part="a1")
    path(f"M{_n(cx + R)} {_n(TOP)} V{_n(BASE)}", part="a1")
    if full:
        # Les deux aiguilles en un seul trace : un trait seul, sans largeur ou
        # sans hauteur, ne peut pas recevoir le degrade.
        path(f"M{_n(cx)} {_n(CY - 17)} V{_n(CY)} H{_n(cx + 13)}", role="detail", width=DETAIL_W, part="clock")
    x = cx + R + W / 2 + GAP

    # i : point de l'assistant
    s = x + W / 2
    path(f"M{_n(s)} {_n(TOP + 22)} V{_n(BASE)}", part="i")
    circle(s, TOP - 34, DOT_R, "dot", part="i")
    x = s + W / 2 + GAP

    # a : coche
    cx = x + R + W / 2
    circle(cx, CY, R, "ink", part="a2")
    path(f"M{_n(cx + R)} {_n(TOP)} V{_n(BASE)}", part="a2")
    if full:
        path(
            f"M{_n(cx - 13)} {_n(CY + 1)} L{_n(cx - 3)} {_n(CY + 11)} L{_n(cx + 14)} {_n(CY - 10)}",
            role="detail", width=DETAIL_W, part="check",
        )
    x = cx + R + W / 2
    return items, x + 20


VIEW_Y, VIEW_H = 20, 170


def _gradient(uid):
    """Degrade indigo vers cyan, en coordonnees de l'element qui l'utilise."""
    return (f'<linearGradient id="{uid}" x1="0" x2="1" y1="0" y2="0">'
            f'<stop offset="0" stop-color="{INDIGO}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>')


def _element(item, colors, uid, ring_pct, extra=None):
    """Rend un element en SVG avec les couleurs donnees.

    :param extra: attributs supplementaires ; None pour un logo fixe (sans classes)
    """
    role = item["role"]
    stroke = {"ink": colors["ink"], "track": colors["track"], "ring": f"url(#{uid})",
              "detail": f"url(#{uid})"}.get(role)
    cls = f' class="{item["part"]} {role}"' if extra is not None else ""
    extra = extra or ""
    if item["kind"] == "circle":
        if role == "dot":
            return f'<circle{cls} cx="{_n(item["cx"])}" cy="{_n(item["cy"])}" r="{item["r"]}" fill="{CYAN}"{extra}/>'
        ring = ""
        if role == "ring":
            ring = (f' pathLength="100" stroke-linecap="round" stroke-dasharray="{ring_pct} 100"'
                    f' transform="rotate(-90 {_n(item["cx"])} {_n(item["cy"])})"')
        return (f'<circle{cls} cx="{_n(item["cx"])}" cy="{_n(item["cy"])}" r="{_n(item["r"])}" fill="none"'
                f' stroke="{stroke}" stroke-width="{W}"{ring}{extra}/>')
    return (f'<path{cls} d="{item["d"]}" fill="none" stroke="{stroke}" stroke-width="{item["width"]}"'
            f' stroke-linecap="round" stroke-linejoin="round"{extra}/>')


def static_svg(full, theme):
    """Logo fixe, couleurs du theme figees."""
    items, width = geometry(full)
    colors = THEMES[theme]
    body = "\n  ".join(_element(item, colors, "g", RING_PCT, extra=None) for item in items)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {VIEW_Y} {_n(width)} {VIEW_H}"'
            f' width="{_n(width)}" height="{VIEW_H}" role="img" aria-label="NOMAIA">\n'
            f"  <defs>{_gradient('g')}</defs>\n  {body}\n</svg>\n")


ANIMATION_CSS = """
  .ink, .detail { stroke-dasharray: 1 1; }
  @keyframes trace { from { stroke-dashoffset: 1; } to { stroke-dashoffset: 0; } }
  @keyframes remplit { from { stroke-dasharray: 0 100; } }
  @keyframes tourne { from { transform: rotate(-360deg); } }
  @keyframes pop { 0% { transform: scale(0); } 70% { transform: scale(1.25); } 100% { transform: scale(1); } }
  .ink { animation: trace 0.7s cubic-bezier(.4,0,.2,1) backwards; }
  .n { animation-delay: 0s; } .a1 { animation-delay: .3s; } .m { animation-delay: .15s; }
  .i { animation-delay: .45s; } .a2 { animation-delay: .6s; }
  .ring { animation: remplit 1.1s cubic-bezier(.2,.8,.2,1) .2s backwards; }
  .clock { transform-box: fill-box; transform-origin: 0% 100%; animation: tourne 1.2s cubic-bezier(.2,.8,.2,1) .7s backwards; }
  .check { animation: trace .45s ease-out 1.2s backwards; }
  .dot { transform-box: fill-box; transform-origin: center; animation: pop .5s cubic-bezier(.34,1.56,.64,1) 1s backwards; }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; } }
"""


def animated_svg(theme):
    """Logo complet anime une fois (README) : les lettres se tracent, l'anneau
    se remplit, l'horloge tourne, la coche se dessine, le point apparait.
    Sans animation, l'etat affiche est l'etat final."""
    items, width = geometry(True)
    colors = THEMES[theme]
    parts = []
    for item in items:
        extra = ""
        if item["role"] in ("ink", "detail") and item["part"] != "clock":
            extra = ' pathLength="1"'
        parts.append(_element(item, colors, "g", RING_PCT, extra=extra))
    body = "\n  ".join(parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {VIEW_Y} {_n(width)} {VIEW_H}"'
            f' width="{_n(width)}" height="{VIEW_H}" role="img" aria-label="NOMAIA">\n'
            f"  <defs>{_gradient('g')}</defs>\n  <style>{ANIMATION_CSS}  </style>\n  {body}\n</svg>\n")


def jinja_macro():
    """Macro de l'interface : couleurs par variables CSS, anneau pilote par --p.

    brand_logo(uid, full=False, label="NOMAIA") ; uid distinct par instance
    (les identifiants de degrade doivent etre uniques dans la page).
    """
    blocks = []
    for full in (False, True):
        items, width = geometry(full)
        parts = []
        for item in items:
            role = item["role"]
            cls = f'{item["part"]} logo-{role}'
            if item["kind"] == "circle":
                cx, cy = _n(item["cx"]), _n(item["cy"])
                if role == "dot":
                    parts.append(f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="{item["r"]}"/>')
                elif role == "ring":
                    parts.append(
                        f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="{_n(item["r"])}" pathLength="100"'
                        f' stroke="url(#{{{{ uid }}}}-g)" transform="rotate(-90 {cx} {cy})"/>')
                else:
                    parts.append(f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="{_n(item["r"])}"/>')
            else:
                stroke = ' stroke="url(#{{ uid }}-g)"' if role == "detail" else ""
                parts.append(f'<path class="{cls}" d="{item["d"]}"{stroke}/>')
        svg = (f'<svg class="logo" viewBox="0 {VIEW_Y} {_n(width)} {VIEW_H}" role="img" aria-label="{{{{ label }}}}">'
               f'<defs>{_gradient("{{ uid }}-g")}</defs>' + "".join(parts) + "</svg>")
        blocks.append(svg)
    ring = (f'<svg class="logo logo-mark" viewBox="0 0 100 100" role="img" aria-label="{{{{ label }}}}">'
            f'<defs>{_gradient("{{ uid }}-g")}</defs>'
            f'<circle class="o logo-track" cx="50" cy="50" r="{_n(R)}"/>'
            f'<circle class="o logo-ring" cx="50" cy="50" r="{_n(R)}" pathLength="100"'
            f' stroke="url(#{{{{ uid }}}}-g)" transform="rotate(-90 50 50)"/></svg>')
    return (
        "{#- Genere par tools/logo.py : ne pas modifier a la main. -#}\n"
        '{% macro brand_logo(uid, full=False, label="NOMAIA") -%}\n'
        "{% if full %}" + blocks[1] + "{% else %}" + blocks[0] + "{% endif %}\n"
        "{%- endmacro %}\n\n"
        "{#- L'anneau seul : petites tailles (barre du haut sur telephone). -#}\n"
        '{% macro brand_ring(uid, label="NOMAIA") -%}\n' + ring + "\n{%- endmacro %}\n"
    )


def main():
    """Ecrit les logos fixes, le logo anime et la macro de l'interface."""
    out = ROOT / "docs" / "logo"
    out.mkdir(parents=True, exist_ok=True)
    for name, full in (("complet", True), ("sobre", False)):
        for theme in THEMES:
            (out / f"nomaia-{name}-{theme}.svg").write_text(static_svg(full, theme), encoding="utf-8")
    for theme in THEMES:
        (out / f"nomaia-anime-{theme}.svg").write_text(animated_svg(theme), encoding="utf-8")
    (ROOT / "templates" / "_logo.html").write_text(jinja_macro(), encoding="utf-8")
    print("Logos ecrits dans", out, "et templates/_logo.html")


if __name__ == "__main__":
    main()
