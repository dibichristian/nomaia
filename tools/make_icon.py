"""Genere l'icone de NOMAIA (PNG pour la barre des taches, ICO pour les raccourcis).

L'icone reprend l'anneau d'avancement du logo (tools/logo.py) : un anneau
rempli aux trois quarts, degrade indigo vers cyan, sur une tuile bleu nuit.

Ecrit aussi les icones de l'ecran d'accueil du telephone (manifeste web et
apple-touch-icon) : carre plein, sans transparence, l'anneau dans la zone
sure de 80 % que les lanceurs Android rognent en cercle ou en goutte.

A relancer uniquement si l'identite visuelle change :
    python tools/make_icon.py
"""
import sys
from math import cos, radians, sin
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

TILE = (11, 16, 32, 255)        # #0b1020
TRACK = (51, 65, 85, 255)       # #334155
INDIGO = (99, 102, 241)         # #6366f1
CYAN = (34, 211, 238)           # #22d3ee
RING_PCT = 72                   # meme remplissage que les logos fixes
SIZE = 256
SUPERSAMPLE = 4                 # dessin en grand puis reduction : bords lisses


# Icones de l'ecran d'accueil : nom de fichier et cote en pixels.
APP_ICONS = (("app-192.png", 192), ("app-512.png", 512), ("apple-touch-icon.png", 180))


def _gradient(size):
    """Degrade horizontal indigo vers cyan, de la taille de l'icone."""
    band = Image.new("RGBA", (size, 1))
    for x in range(size):
        t = x / max(size - 1, 1)
        band.putpixel((x, 0), tuple(round(a + (b - a) * t) for a, b in zip(INDIGO, CYAN)) + (255,))
    return band.resize((size, size))


def build_image(size=SIZE, full_bleed=False, ring_ratio=0.62):
    """Dessine l'anneau d'avancement sur une tuile bleu nuit.

    :param full_bleed: carre plein (icone d'ecran d'accueil, que le systeme
        arrondit lui-meme) plutot qu'un carre arrondi sur fond transparent
    :param ring_ratio: diametre exterieur de l'anneau rapporte au cote
    """
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if full_bleed:
        draw.rectangle([0, 0, big - 1, big - 1], fill=TILE)
    else:
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=TILE)

    outer = big * ring_ratio
    width = outer * 0.21
    center = big / 2
    radius = (outer - width) / 2    # rayon de l'axe du trait
    box = [center - outer / 2, center - outer / 2, center + outer / 2, center + outer / 2]
    draw.ellipse(box, outline=TRACK, width=round(width))

    # Arc de l'anneau : masque dessine puis rempli par le degrade.
    mask = Image.new("L", (big, big), 0)
    mask_draw = ImageDraw.Draw(mask)
    start, end = -90, -90 + 360 * RING_PCT / 100
    mask_draw.arc(box, start, end, fill=255, width=round(width))
    for angle in (start, end):
        # Bouts arrondis, comme stroke-linecap="round".
        x = center + radius * cos(radians(angle))
        y = center + radius * sin(radians(angle))
        mask_draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=255)
    image.paste(_gradient(big), (0, 0), mask)
    return image.resize((size, size), Image.LANCZOS)


def main():
    """Ecrit icon.png, icon.ico et les icones d'ecran d'accueil dans static/img."""
    folder = config.BASE_DIR / "static" / "img"
    folder.mkdir(parents=True, exist_ok=True)

    image = build_image()
    image.save(folder / "icon.png")
    image.save(
        folder / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    for name, size in APP_ICONS:
        build_image(size, full_bleed=True, ring_ratio=0.5).convert("RGB").save(folder / name)
    print("Icones ecrites dans", folder)


if __name__ == "__main__":
    main()
