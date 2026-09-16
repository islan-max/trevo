#!/usr/bin/env bash
# Alerta quando um arquivo rastreável do projeto está sendo silenciosamente
# ignorado pelo .gitignore.
#
# Existe por causa de um incidente real: `app/core/secrets.py` casou com a regra
# `secrets.*` (que protege contra commitar segredos, e está correta). O
# `git add -A` pulou o arquivo sem dizer nada, o deploy subiu sem ele e a
# produção inteira caiu com ModuleNotFoundError.
#
# Uso: ./scripts/check-ignored-files.sh [diretórios...]

set -euo pipefail

if [ "$#" -gt 0 ]; then
  DIRS=("$@")
else
  DIRS=(app frontend/app frontend/components frontend/lib migrations scripts tests)
fi
FOUND=0

# csv/pdf entram porque são os formatos dos dois recursos centrais do produto
# (importação e relatórios) e o .gitignore os ignora por padrão para não
# versionar exportações geradas — a mesma regra que engoliria uma fixture de
# teste sem avisar.
for dir in "${DIRS[@]}"; do
  [ -d "$dir" ] || continue
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    # Arquivos de build e dependência são ignorados de propósito.
    case "$file" in
      */node_modules/*|*/__pycache__/*|*/.next/*|*/out/*|*.pyc) continue ;;
    esac
    echo "  $file"
    FOUND=1
  done < <(git ls-files --others --ignored --exclude-standard -- "$dir" 2>/dev/null | grep -E '\.(py|ts|tsx|sql|css|csv|pdf|json|mjs|mts|yml|yaml)$' || true)
done

if [ "$FOUND" -eq 1 ]; then
  cat <<'EOF'

Os arquivos acima são código-fonte e estão sendo ignorados pelo .gitignore.
Se algum deles precisa ir para o repositório, renomeie-o ou ajuste a regra —
não force o add sem entender qual regra o pegou:

  git check-ignore -v <arquivo>

EOF
  exit 1
fi

echo "Nenhum arquivo de código ignorado por engano."
