from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import HTTPException

# CSV-05: só ";" e "," eram reconhecidos. Extratos de alguns bancos e
# planilhas exportadas usam tabulação ou pipe.
_CSV_DELIMITER_CANDIDATES = (";", ",", "\t", "|")

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")
CSV_IMPORT_MAX_ROWS = 5000


def _count_delimiters(line: str) -> dict[str, int]:
    return {delimiter: line.count(delimiter) for delimiter in _CSV_DELIMITER_CANDIDATES}



def detect_csv_delimiter(sample: str) -> str:
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    counts = _count_delimiters(first_line)
    best_delimiter = max(counts, key=lambda delimiter: counts[delimiter])
    return best_delimiter if counts[best_delimiter] > 0 else ","



def find_csv_header_line_index(lines: list[str]) -> int:
    """Localiza a linha de cabeçalho real, pulando o preâmbulo que extratos
    bancários costumam trazer antes da tabela (nome do banco, período,
    agência) — CSV-04.

    Heurística: a primeira linha cujo delimitador mais frequente também
    aparece na próxima linha não vazia, com a MESMA contagem de campos.
    Preâmbulo tipicamente não usa o delimitador do arquivo, ou usa em
    quantidade diferente da linha de dados seguinte — a linha de cabeçalho
    de verdade e a primeira linha de dados sempre têm o mesmo número de
    colunas. Sem nenhuma linha assim, cai no comportamento antigo: a
    primeira linha é o cabeçalho.
    """
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        counts = _count_delimiters(line)
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        if count == 0:
            continue
        field_count = len(line.split(delimiter))
        next_line = next((candidate for candidate in lines[index + 1 :] if candidate.strip()), None)
        if next_line is not None and len(next_line.split(delimiter)) == field_count:
            return index
    return 0



def parse_csv_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = None
    # CSV-06: cp1252 antes do fallback final. latin-1 nunca levanta
    # UnicodeDecodeError (mapeia todo byte para um caractere), o que fazia um
    # arquivo Windows-1252 (aspas curvas, travessão) "funcionar" decodificado
    # errado em vez de cair no encoding certo.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("latin-1")

    lines = text.splitlines()
    header_index = find_csv_header_line_index(lines)
    delimiter = detect_csv_delimiter(lines[header_index] if lines else "")
    table_text = "\n".join(lines[header_index:])

    reader = csv.DictReader(io.StringIO(table_text), delimiter=delimiter)
    columns = [column.strip() for column in (reader.fieldnames or []) if column and column.strip()]
    if not columns:
        raise HTTPException(status_code=400, detail="CSV sem cabeçalho.")

    rows: list[dict[str, str]] = []
    for index, row in enumerate(reader, start=1):
        if index > CSV_IMPORT_MAX_ROWS:
            raise HTTPException(status_code=400, detail=f"CSV excede o limite de {CSV_IMPORT_MAX_ROWS} linhas.")
        cleaned = {
            str(key or "").strip(): unescape_csv_formula_guard(str(value or "").strip())
            for key, value in row.items()
            if key
        }
        if any(cleaned.values()):
            rows.append(cleaned)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV sem linhas para importar.")
    return columns, rows



def csv_safe_cell(value: Any) -> str:
    text = str(value or "")
    if text and text[0] in CSV_FORMULA_PREFIXES:
        return f"'{text}"
    return text



def unescape_csv_formula_guard(value: str) -> str:
    """Desfaz o apóstrofo de guarda de csv_safe_cell ao reimportar um CSV
    exportado pelo próprio Trevo — sem isto, ele volta como caractere
    literal na descrição (CSV-07). Só remove quando o caractere seguinte é
    exatamente um dos gatilhos de fórmula, o mesmo critério que decide
    adicioná-lo na exportação — não mexe num apóstrofo comum.
    """
    if len(value) >= 2 and value[0] == "'" and value[1] in CSV_FORMULA_PREFIXES:
        return value[1:]
    return value

