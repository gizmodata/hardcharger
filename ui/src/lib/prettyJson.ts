// Pretty-printing JSON text without parsing it into JavaScript values, so every number keeps
// its digits (a parse would round a 20-digit integer or a 38-digit decimal to a double).

/** Whether `text` is one JSON object or array (what the cell inspector pretty-prints). */
export function looksLikeJson(text: string): boolean {
  const t = text.trim();
  if (!((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]")))) return false;
  try {
    JSON.parse(t);
    return true;
  } catch {
    return false;
  }
}

/** JSON text re-indented by `indent` spaces, its strings and numbers as they were. */
export function prettyJson(text: string, indent = 2): string {
  let out = "";
  let depth = 0;
  let inString = false;
  const pad = () => `\n${" ".repeat(depth * indent)}`;
  const t = text.trim();
  for (let i = 0; i < t.length; i++) {
    const c = t[i]!;
    if (inString) {
      out += c;
      if (c === "\\") {
        out += t[i + 1] ?? "";
        i++;
      } else if (c === '"') inString = false;
      continue;
    }
    switch (c) {
      case '"':
        inString = true;
        out += c;
        break;
      case "{":
      case "[": {
        const close = c === "{" ? "}" : "]";
        // An empty object or array stays on one line.
        let j = i + 1;
        while (j < t.length && /\s/.test(t[j]!)) j++;
        if (t[j] === close) {
          out += c + close;
          i = j;
        } else {
          depth++;
          out += c + pad();
        }
        break;
      }
      case "}":
      case "]":
        depth--;
        out += pad() + c;
        break;
      case ",":
        out += c + pad();
        break;
      case ":":
        out += ": ";
        break;
      default:
        if (!/\s/.test(c)) out += c;
    }
  }
  return out;
}
