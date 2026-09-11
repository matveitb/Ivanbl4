#!/bin/sh
# Развернуть Ghidra с модулем C166 и разобрать прошивку без графики.
#
# Модуль собран под 12.1.2, ставится и в 12.1.3 -- достаточно поправить
# версию в extension.properties, проверок совместимости больше нет.
#
# Внимание: в Ghidra 12 Jython убран. Скрипты пишутся на Java или под
# PyGhidra; .py-скрипт в headless падает с JythonStubException.
set -eu

VER=12.1.3
BUILD=20260817
WORK=${1:-/tmp/ghidra-kia}
REPO=$(cd "$(dirname "$0")/../.." && pwd)

mkdir -p "$WORK"
cd "$WORK"

[ -f ghidra.zip ] || curl -sSL --max-time 900 -o ghidra.zip \
  "https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_${VER}_build/ghidra_${VER}_PUBLIC_${BUILD}.zip"
[ -d "ghidra_${VER}_PUBLIC" ] || unzip -q ghidra.zip
G="$WORK/ghidra_${VER}_PUBLIC"

if [ ! -d "$G/Ghidra/Extensions/c166-ghidra-module" ]; then
  mkdir -p mod "$G/Ghidra/Extensions"
  unzip -q -o "$REPO"/firmware/*c166-ghidra-module.zip -d mod
  cp -r mod/c166-ghidra-module "$G/Ghidra/Extensions/"
  sed -i "s/^version=.*/version=${VER}/" "$G/Ghidra/Extensions/c166-ghidra-module/extension.properties"
fi

FW=${FW:-"$REPO/firmware/FBH3ID60_stok.bin"}
cp "$FW" "$WORK/target.bin"

"$G/support/analyzeHeadless" "$WORK/proj" kia \
  -import "$WORK/target.bin" -overwrite \
  -loader BinaryLoader -loader-baseAddr 0x800000 \
  -processor "C166:LE:16:default" -cspec tasking

echo
echo "Готово. Декомпилировать функции:"
echo "  \"$G/support/analyzeHeadless\" \"$WORK/proj\" kia -process target.bin \\"
echo "     -scriptPath $REPO/tools/ghidra/scripts \\"
echo "     -postScript DecompileTargets.java 55B5E 3E66C 3BF26 $WORK/decomp.txt"
