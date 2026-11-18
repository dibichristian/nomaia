/* Rendu Markdown maison, sans dependance : window.JMarkdown.render(text) -> html.
 *
 * Securite : le texte entier est echappe (&, <, >) AVANT toute mise en forme.
 * Seules les balises ecrites ici sortent du rendu ; aucun HTML de la note ne
 * passe. Le seul attribut tire du texte est le href d'un lien : il n'accepte
 * ni espace ni guillemet (impossible d'en sortir) et seulement http(s):,
 * mailto: ou un chemin commencant par « / ». Sinon le lien reste du texte.
 *
 * Pris en charge : titres # a ###, paragraphes (retours a la ligne gardes),
 * gras, italique, code en ligne, blocs ```lang colores (python, xml, js, sql),
 * listes a puces et numerotees (imbriquees par indentation), cases [ ] / [x],
 * citations >, liens [texte](url), regle ---, tableaux simples avec |.
 */
(function () {
  "use strict";

  function escape(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* ---------- Coloration syntaxique (sur du texte deja echappe) ---------- */

  const STR_DQ = /"(?:\\.|[^"\\\n])*"/.source;
  const STR_SQ = /'(?:\\.|[^'\\\n])*'/.source;
  const NUMBER = /\b\d+(?:\.\d+)?\b/.source;
  const words = (list) => "\\b(?:" + list.split(" ").join("|") + ")\\b";

  const GRAMMARS = {
    python: {
      c: /#[^\n]*/.source,
      s: [/"""[\s\S]*?"""/.source, /'''[\s\S]*?'''/.source, STR_DQ, STR_SQ].join("|"),
      k: words("and as assert async await break class continue def del elif else except False finally for from" +
        " global if import in is lambda None nonlocal not or pass raise return self super True try while with yield"),
    },
    js: {
      c: /\/\/[^\n]*|\/\*[\s\S]*?\*\//.source,
      s: [STR_DQ, STR_SQ, /`(?:\\.|[^`\\])*`/.source].join("|"),
      k: words("async await break case catch class const continue default delete do else export extends false" +
        " finally for from function if import in instanceof let new null of return static super switch this" +
        " throw true try typeof undefined var void while yield"),
    },
    sql: {
      c: /--[^\n]*|\/\*[\s\S]*?\*\//.source,
      s: STR_SQ,
      k: words("select from where and or not insert into values update set delete create table alter drop index" +
        " join left right inner outer on group by order having limit offset as distinct union all case when" +
        " then else end null is in like ilike between exists returning begin commit rollback primary key" +
        " references default cascade with"),
      flags: "i",
    },
    // Les chevrons sont deja echappes : on reconnait &lt;balise et /&gt;.
    xml: {
      c: /&lt;!--[\s\S]*?--&gt;/.source,
      s: STR_DQ,
      k: /&lt;\/?[\w:.-]+|\/?&gt;/.source,
    },
  };
  const LANGS = {
    py: "python", python: "python", xml: "xml", html: "xml", qweb: "xml", js: "js", javascript: "js",
    owl: "js", ts: "js", sql: "sql", psql: "sql", postgres: "sql", postgresql: "sql",
  };

  function highlight(code, lang) {
    const grammar = GRAMMARS[LANGS[lang]];
    if (!grammar) return code;
    const pattern = new RegExp(
      "(" + grammar.c + ")|(" + grammar.s + ")|(" + grammar.k + ")|(" + NUMBER + ")", "g" + (grammar.flags || "")
    );
    return code.replace(pattern, (match, comment, string, keyword) => {
      const kind = comment ? "c" : string ? "s" : keyword ? "k" : "n";
      return '<span class="md-' + kind + '">' + match + "</span>";
    });
  }

  /* ---------- Mise en forme en ligne ---------- */

  function safeUrl(url) {
    // « //hote » et « /\hote » meneraient hors du site : refuses.
    return /^(?:https?:\/\/|mailto:)/i.test(url) || /^\/(?![/\\])/.test(url);
  }

  function inline(text) {
    // Code et balises de lien mis de cote : le gras et l'italique n'y touchent pas.
    const kept = [];
    const hold = (html) => "\u0000" + (kept.push(html) - 1) + "\u0000";
    let out = text.replace(/`([^`\n]+)`/g, (match, code) => hold("<code>" + code + "</code>"));
    out = out.replace(/\[([^\]\n]+)\]\(([^\s()"'<>]+)\)/g, (match, label, url) => {
      if (!safeUrl(url)) return match;
      const external = /^https?:/i.test(url) ? ' rel="noopener noreferrer" target="_blank"' : "";
      return hold('<a href="' + url + '"' + external + ">") + label + hold("</a>");
    });
    out = out
      .replace(/\*\*(?=\S)([^\n]*?\S)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w])__(?=\S)([^\n]*?\S)__(?!\w)/g, "$1<strong>$2</strong>")
      .replace(/\*(?=[^\s*])([^*\n]*?[^\s*])\*/g, "<em>$1</em>")
      .replace(/(^|[^\w])_(?=\S)([^_\n]*?\S)_(?!\w)/g, "$1<em>$2</em>");
    return out.replace(/\u0000(\d+)\u0000/g, (match, index) => kept[Number(index)]);
  }

  /* ---------- Blocs ---------- */

  const FENCE = /^\s*```\s*([\w+-]*)\s*$/;
  const HEADING = /^(#{1,3})\s+(.+?)\s*#*\s*$/;
  const RULE = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
  const QUOTE = /^\s*&gt;\s?/;
  const LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

  const indentOf = (line) => line.match(/^ */)[0].length;
  const isOrdered = (marker) => /\d/.test(marker);

  function isTable(lines, i) {
    return lines[i].includes("|") && i + 1 < lines.length && lines[i + 1].includes("|") && TABLE_SEP.test(lines[i + 1]);
  }

  function startsBlock(lines, i) {
    const line = lines[i];
    return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) ||
      LIST.test(line) || isTable(lines, i);
  }

  function cells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => inline(cell.trim()));
  }

  function table(lines, i) {
    const head = cells(lines[i]);
    const rows = [];
    i += 2;
    while (i < lines.length && lines[i].trim() && lines[i].includes("|")) rows.push(cells(lines[i++]));
    const row = (values, tag) => "<tr>" + head.map((_, k) => "<" + tag + ">" + (values[k] || "") + "</" + tag + ">").join("") + "</tr>";
    return {
      html: '<div class="md-table"><table><thead>' + row(head, "th") + "</thead><tbody>" +
        rows.map((values) => row(values, "td")).join("") + "</tbody></table></div>",
      next: i,
    };
  }

  function list(lines, i) {
    const first = lines[i].match(LIST);
    const base = first[1].length;
    const ordered = isOrdered(first[2]);
    const items = [];
    while (i < lines.length) {
      const line = lines[i];
      const match = line.match(LIST);
      if (match && match[1].length === base && isOrdered(match[2]) === ordered) {
        items.push({ text: match[3], children: [] });
        i++;
      } else if (line.trim() && indentOf(line) > base) {
        items[items.length - 1].children.push(line); // sous-liste ou suite de l'element
        i++;
      } else if (!line.trim() && i + 1 < lines.length && LIST.test(lines[i + 1]) && indentOf(lines[i + 1]) >= base) {
        i++; // ligne vide entre deux elements : la liste continue
      } else {
        break;
      }
    }
    const start = ordered ? parseInt(first[2], 10) : 1;
    const open = ordered ? (start !== 1 ? '<ol start="' + start + '">' : "<ol>") : "<ul>";
    const html = items.map((item) => {
      const task = item.text.match(/^\[([ xX])\]\s+(.*)$/);
      let body = task
        ? '<input type="checkbox" disabled' + (task[1] === " " ? "" : " checked") + "> " + inline(task[2])
        : inline(item.text);
      if (item.children.length) {
        const cut = Math.min(...item.children.map(indentOf));
        body += blocks(item.children.map((line) => line.slice(cut)));
      }
      return (task ? '<li class="md-task">' : "<li>") + body + "</li>";
    }).join("");
    return { html: open + html + (ordered ? "</ol>" : "</ul>"), next: i };
  }

  function blocks(lines) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let match;
      if (!line.trim()) {
        i++;
      } else if ((match = line.match(FENCE))) {
        const lang = match[1].toLowerCase();
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) code.push(lines[i++]);
        i++; // barriere fermante (ou fin du texte)
        out.push('<pre class="md-pre"><code' + (lang ? ' class="lang-' + lang + '"' : "") + ">" +
          highlight(code.join("\n"), lang) + "</code></pre>");
      } else if ((match = line.match(HEADING))) {
        const level = match[1].length;
        out.push("<h" + level + ">" + inline(match[2]) + "</h" + level + ">");
        i++;
      } else if (RULE.test(line)) {
        out.push("<hr>");
        i++;
      } else if (QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length && QUOTE.test(lines[i])) inner.push(lines[i++].replace(QUOTE, ""));
        out.push("<blockquote>" + blocks(inner) + "</blockquote>");
      } else if (isTable(lines, i) || LIST.test(line)) {
        const result = isTable(lines, i) ? table(lines, i) : list(lines, i);
        out.push(result.html);
        i = result.next;
      } else {
        const para = [line.trim()];
        i++;
        while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) para.push(lines[i++].trim());
        out.push("<p>" + para.map(inline).join("<br>") + "</p>");
      }
    }
    return out.join("\n");
  }

  function render(text) {
    const source = String(text == null ? "" : text)
      .replace(/\u0000/g, "")
      .replace(/\r\n?/g, "\n")
      .replace(/\t/g, "    ");
    return blocks(escape(source).split("\n"));
  }

  window.JMarkdown = { render };
})();
