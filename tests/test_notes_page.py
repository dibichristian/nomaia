"""Page de la base de connaissances (/notes) et rendu Markdown (static/js/markdown.js)."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from models import knowledge
from tests.base import NomaiaTestCase

MARKDOWN_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "markdown.js"
NODE = shutil.which("node")

# Charge markdown.js dans un bac a sable Node (window factice) et rend chaque entree recue sur stdin.
NODE_RUNNER = """
const fs = require("fs"), vm = require("vm");
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const inputs = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(inputs.map((text) => sandbox.window.JMarkdown.render(text))));
"""


class NotesPageTest(NomaiaTestCase):
    def test_page_liste_rend_le_gabarit_et_ses_ressources(self):
        reponse = self.client().get("/notes")
        self.assertEqual(reponse.status_code, 200)
        html = reponse.get_data(as_text=True)
        for marqueur in ("css/pages/notes.css", "js/markdown.js", "js/notes.js", "js/attachments.js", 'id="nt-layout"',
                         'data-note-id=""', 'id="nt-confirm"', "Base de connaissances"):
            self.assertIn(marqueur, html)

    def test_page_note_porte_son_identifiant(self):
        note = knowledge.create({"title": "Vue form héritée", "content": "# Titre"})
        reponse = self.client().get(f"/notes/{note['id']}")
        self.assertEqual(reponse.status_code, 200)
        html = reponse.get_data(as_text=True)
        self.assertIn(f'data-note-id="{note["id"]}"', html)
        self.assertIn("css/pages/notes.css", html)
        self.assertIn("js/markdown.js", html)

    def test_page_exige_la_cle_d_acces(self):
        reponse = self.app.test_client().get("/notes")
        self.assertNotEqual(reponse.status_code, 200)


@unittest.skipUnless(NODE, "node absent : rendu Markdown non verifie")
class MarkdownRenderTest(unittest.TestCase):
    @staticmethod
    def render(*inputs):
        """Rend les textes avec markdown.js sous Node ; renvoie la liste des HTML."""
        result = subprocess.run(
            [NODE, "-e", NODE_RUNNER, str(MARKDOWN_JS)],
            input=json.dumps(list(inputs)), capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_entrees_hostiles_neutralisees(self):
        hostiles = [
            "<script>alert(1)</script>",
            "[x](javascript:alert(1))",
            "[x](JaVaScRiPt:alert(1))",
            "<img onerror>",
            "<img src=x onerror=alert(1)>",
            '[x](https://ok.example" onclick="alert(1))',
            "[x](//evil.example) [y](/\\evil.example) [z](data:text/html,coucou)",
            "```python\n<script>alert(1)</script>\n```",
            "| a | b |\n|---|---|\n| <svg onload=alert(1)> | `<b>` |",
        ]
        for texte, html in zip(hostiles, self.render(*hostiles)):
            with self.subTest(texte=texte):
                self.assertNotRegex(html, r"(?i)<script")
                self.assertNotRegex(html, r"(?i)<(img|svg)\b")
                self.assertNotRegex(html, r"(?i)href\s*=\s*[\"']?\s*(javascript|data):")
                # Aucun gestionnaire d'evenement dans une balise produite.
                self.assertNotRegex(html, r"(?i)<[^>]*\son\w+\s*=")
                for href in re.findall(r'href="([^"]*)"', html):
                    self.assertRegex(href, r"^(https?://|mailto:|/(?![/\\]))")
        self.assertNotIn("onerror=", self.render("<img onerror>")[0])

    def test_liens_autorises(self):
        html = self.render("[doc](https://odoo.example/a_b) [plan](/plan?tache=3) [mail](mailto:a@b.example)")[0]
        self.assertIn('<a href="https://odoo.example/a_b" rel="noopener noreferrer" target="_blank">doc</a>', html)
        self.assertIn('<a href="/plan?tache=3">plan</a>', html)
        self.assertIn('<a href="mailto:a@b.example">mail</a>', html)

    def test_blocs_et_coloration(self):
        texte = (
            "# Titre\n**gras** et *italique* avec `code`\n\n"
            "- [ ] a faire\n- [x] fait\n\n1. un\n2. deux\n\n> citation\n\n---\n\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "```python\n# commentaire\ndef f():\n    return \"x\", 42\n```"
        )
        html = self.render(texte)[0]
        for fragment in ("<h1>Titre</h1>", "<strong>gras</strong>", "<em>italique</em>", "<code>code</code>",
                         '<input type="checkbox" disabled>', '<input type="checkbox" disabled checked>',
                         "<ol>", "<blockquote>", "<hr>", "<table>", "<th>A</th>",
                         '<span class="md-c"># commentaire</span>', '<span class="md-k">def</span>',
                         '<span class="md-s">"x"</span>', '<span class="md-n">42</span>'):
            self.assertIn(fragment, html)
