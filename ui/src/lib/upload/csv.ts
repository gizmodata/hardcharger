// A streaming CSV reader (RFC 4180 and what spreadsheets write): fields split by a delimiter of
// one to four characters, quoted with " (a doubled "" inside is one "), line breaks inside
// quotes kept, records ended by \n, \r\n or \r. An unquoted empty field is NULL; a quoted one
// is the empty string. Text arrives in chunks of any size, so a file is read in bounded memory.
// The dialect (delimiter) is sniffed from the start of the file as DuckDB's sniffer does: the
// candidate that splits the most lines into the same number of fields wins.

export class CsvError extends Error {}

/** The longest field read (a guard against a quote that never closes). */
export const maxFieldChars = 16 << 20;
/** The most fields one record may have (Excel's columns). */
export const maxFields = 16_384;

export type CsvRecord = (string | null)[];

/**
 * Splits CSV text into records. `onRecord` gets each record's fields and the line it began on
 * (1-based). Blank lines are skipped.
 */
export class CsvParser {
  readonly #delimiter: string;
  readonly #onRecord: (fields: CsvRecord, line: number) => void;
  #fields: CsvRecord = [];
  #field = "";
  #quoted = false; // the current field was quoted
  #inQuotes = false;
  #quoteSeen = false; // a quote inside quotes: either the end or the first of ""
  #fieldStarted = false;
  #pendingCr = false;
  #line = 1;
  #recordLine = 1;
  #pendingDelimiter = ""; // the part of a multi-character delimiter seen so far

  constructor(delimiter: string, onRecord: (fields: CsvRecord, line: number) => void) {
    if (delimiter.length < 1 || delimiter.length > 4) throw new CsvError("A delimiter is 1 to 4 characters.");
    if (delimiter.includes('"') || /[\r\n]/.test(delimiter)) {
      throw new CsvError("The delimiter can't be a quote or a line break.");
    }
    this.#delimiter = delimiter;
    this.#onRecord = onRecord;
  }

  /** The line the parser is on (1-based). */
  get line(): number {
    return this.#line;
  }

  #endField(): void {
    if (this.#fields.length >= maxFields) {
      throw new CsvError(
        `Line ${this.#recordLine.toLocaleString()} has more than ${maxFields.toLocaleString()} fields.`,
      );
    }
    this.#fields.push(this.#quoted || this.#field !== "" ? this.#field : null);
    this.#field = "";
    this.#quoted = false;
    this.#fieldStarted = false;
  }

  #endRecord(): void {
    this.#endField();
    const fields = this.#fields;
    this.#fields = [];
    if (!(fields.length === 1 && fields[0] === null)) this.#onRecord(fields, this.#recordLine);
  }

  #newline(): void {
    this.#line++;
  }

  #append(text: string): void {
    this.#field += text;
    if (this.#field.length > maxFieldChars) {
      throw new CsvError(
        `Line ${this.#recordLine.toLocaleString()} has a field longer than ${maxFieldChars >> 20} MiB characters: ` +
          "is the delimiter right, and is every quote closed?",
      );
    }
  }

  push(text: string): void {
    const d = this.#delimiter;
    const d0 = d[0]!;
    let i = 0;
    const n = text.length;
    while (i < n) {
      const c = text[i]!;
      if (this.#pendingCr) {
        this.#pendingCr = false;
        if (c === "\n") {
          i++;
          continue;
        }
      }
      if (this.#inQuotes) {
        if (this.#quoteSeen) {
          this.#quoteSeen = false;
          if (c === '"') {
            this.#append('"');
            i++;
            continue;
          }
          this.#inQuotes = false; // the closing quote; c is handled below
        } else {
          // Copy up to the next quote in one go.
          const q = text.indexOf('"', i);
          const end = q === -1 ? n : q;
          if (end > i) {
            const chunk = text.slice(i, end);
            for (let k = chunk.indexOf("\n"); k !== -1; k = chunk.indexOf("\n", k + 1)) this.#newline();
            if (!chunk.includes("\n")) {
              for (let k = chunk.indexOf("\r"); k !== -1; k = chunk.indexOf("\r", k + 1)) this.#newline();
            }
            this.#append(chunk);
          }
          if (q === -1) return;
          this.#quoteSeen = true;
          i = q + 1;
          continue;
        }
      }
      if (this.#pendingDelimiter) {
        const want = d.slice(this.#pendingDelimiter.length);
        if (c === want[0]) {
          this.#pendingDelimiter += c;
          i++;
          if (this.#pendingDelimiter === d) {
            this.#pendingDelimiter = "";
            this.#endField();
          }
          continue;
        }
        this.#append(this.#pendingDelimiter);
        this.#pendingDelimiter = "";
      }
      if (c === d0) {
        if (d.length === 1) {
          this.#endField();
          i++;
          continue;
        }
        this.#pendingDelimiter = c;
        i++;
        continue;
      }
      if (c === "\n" || c === "\r") {
        this.#endRecord();
        this.#newline();
        this.#recordLine = this.#line;
        if (c === "\r") this.#pendingCr = true;
        i++;
        continue;
      }
      if (c === '"' && !this.#fieldStarted && !this.#quoted) {
        this.#inQuotes = true;
        this.#quoted = true;
        this.#fieldStarted = true;
        i++;
        continue;
      }
      if (this.#quoted) {
        // Text after a closing quote: kept, as DuckDB's lenient readers and spreadsheets do.
        this.#append(c);
        i++;
        continue;
      }
      // Copy unquoted text up to the next special character.
      let j = i + 1;
      while (j < n) {
        const cj = text[j]!;
        if (cj === d0 || cj === "\n" || cj === "\r") break;
        j++;
      }
      this.#append(text.slice(i, j));
      this.#fieldStarted = true;
      i = j;
    }
  }

  /** The end of the text: the last record, if it had no line break after it. */
  end(): void {
    if (this.#inQuotes && !this.#quoteSeen) {
      throw new CsvError(`The quote opened on line ${this.#recordLine.toLocaleString()} is never closed.`);
    }
    this.#inQuotes = false;
    this.#quoteSeen = false;
    if (this.#pendingDelimiter) {
      this.#append(this.#pendingDelimiter);
      this.#pendingDelimiter = "";
    }
    if (this.#fieldStarted || this.#quoted || this.#fields.length > 0) this.#endRecord();
  }
}

/** Parses a whole text (for tests and for sniffing). */
export function parseCsv(
  text: string,
  delimiter: string,
  maxRecords = Infinity,
): { records: CsvRecord[]; lines: number[] } {
  const records: CsvRecord[] = [];
  const lines: number[] = [];
  const stop = new Error("enough");
  const parser = new CsvParser(delimiter, (fields, line) => {
    records.push(fields);
    lines.push(line);
    if (records.length >= maxRecords) throw stop;
  });
  try {
    parser.push(text);
    parser.end();
  } catch (err) {
    if (err !== stop) throw err;
  }
  return { records, lines };
}

/** The delimiters the sniffer tries, in its order of preference on a tie. */
export const candidateDelimiters = [",", "\t", ";", "|"];

/**
 * The delimiter of a CSV file from its first text (a partial last line is left out). A typed
 * delimiter of \t or "tab" means a tab.
 */
export function sniffDelimiter(sample: string, preferTab = false): string {
  const cut = Math.max(sample.lastIndexOf("\n"), 0);
  const text = cut > 0 ? sample.slice(0, cut) : sample;
  const order = preferTab ? ["\t", ...candidateDelimiters.filter((d) => d !== "\t")] : candidateDelimiters;
  let best = order[0]!;
  let bestScore = -1;
  for (const d of order) {
    let records: CsvRecord[];
    try {
      records = parseCsv(text, d, 200).records;
    } catch {
      continue;
    }
    if (records.length === 0) continue;
    const counts = new Map<number, number>();
    for (const r of records) counts.set(r.length, (counts.get(r.length) ?? 0) + 1);
    let modal = 1;
    let modalRows = 0;
    for (const [width, rows] of counts) {
      if (rows > modalRows || (rows === modalRows && width > modal)) {
        modal = width;
        modalRows = rows;
      }
    }
    if (modal < 2) continue;
    // Consistent lines first, then more fields.
    const score = (modalRows / records.length) * 1000 + Math.min(modal, 999);
    if (score > bestScore) {
      best = d;
      bestScore = score;
    }
  }
  return best;
}

/** A delimiter as typed in the dialog: \t and "tab" are a tab; empty means detect. */
export function typedDelimiter(text: string): string | undefined {
  if (text === "") return undefined;
  if (text === "\\t" || text.toLowerCase() === "tab") return "\t";
  return text;
}

/** A delimiter as the dialog shows it. */
export function delimiterLabel(d: string): string {
  if (d === "\t") return "tab";
  if (d === " ") return "space";
  return d;
}
