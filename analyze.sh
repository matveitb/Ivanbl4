#!/usr/bin/env bash
# Полный прогон анализа прошивок Kia Spectra 1.6 / Bosch M7.9.7 (FBH3ID60).
#
#   ./analyze.sh                       # файлы из firmware/ по умолчанию
#   ./analyze.sh сток.bin csok1.bin csok2.bin
#
# Если рядом лежит DAMOS родственной прошивки (firmware/px5ns03d.*), имена,
# описания и масштабы подтягиваются из него. Результаты -- в out/.

set -eu
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/tools:${PYTHONPATH:-}"
mkdir -p out

STOCK="${1:-firmware/FBH3ID60_stok.bin}"
CSOK1="${2:-firmware/FBH3ID60 e2 tun csok.bin}"
CSOK2="${3:-firmware/FBH3ID60 e2 tun csok v2___.bin}"

PROFILE="profiles/FBH3ID60.json"
DAMOS="firmware/px5ns03d.dam"
DAMOS_BIN="firmware/px5ns03d.BIN"
CAL_START=0x10000
CAL_END=0x1C000

for f in "$STOCK" "$CSOK1" "$CSOK2"; do
  [ -f "$f" ] || { echo "Нет файла: $f" >&2; exit 2; }
done

echo "############ 1. ОБЗОР И КОНТРОЛЬНЫЕ СУММЫ ############"
for f in "$STOCK" "$CSOK1" "$CSOK2"; do
  python3 tools/fwinfo.py "$f" > "out/$(basename "${f%.*}")_info.txt"
  printf '  %-40s ' "$(basename "$f")"
  python3 tools/bosch_csum.py check "$f" | tail -1
done

echo
echo "############ 2. СКАНЕРЫ ############"
python3 tools/mapscan.py "$STOCK" --range $CAL_START $CAL_END \
    --min-smooth 0.6 --min-cells 20 --out out/maps_stock.json --top 0 \
    > out/maps_stock.txt 2>&1
grep -E "После|Сырых" out/maps_stock.txt | sed 's/^/  mapscan : /'
python3 tools/gridscan.py "$STOCK" --range $CAL_START $CAL_END \
    --out out/grids_stock.json > out/grids_stock.txt 2>&1
grep -E "Найдено" out/grids_stock.txt | sed 's/^/  gridscan: /'
python3 tools/axisscan.py "$STOCK" --range $CAL_START $CAL_END \
    --chains --min-links 4 --out out/axes_stock.json > out/axes_stock.txt 2>&1
grep -E "Цепочек" out/axes_stock.txt | sed 's/^/  axisscan: /'

XFER_ARG=""
if [ -f "$DAMOS" ] && [ -f "$DAMOS_BIN" ]; then
  echo
  echo "############ 3. DAMOS: ИМЕНА, ОПИСАНИЯ, МАСШТАБЫ ############"
  python3 - <<'PY'
data = open('firmware/px5ns03d.BIN','rb').read()
open('out/px5ns03d_flash.bin','wb').write(data[0x800000:0x880000])
PY
  python3 tools/damos.py "$DAMOS" --json out/damos.json --stats > out/damos.txt 2>&1
  sed -n '1,8p' out/damos.txt
  python3 tools/xfer.py --damos "$DAMOS" --source out/px5ns03d_flash.bin \
      --target "$STOCK" --out out/xfer_FBH3ID60.json
  XFER_ARG="--xfer out/xfer_FBH3ID60.json"
else
  echo
  echo "############ 3. DAMOS не найден -- пропуск ############"
fi

echo
echo "############ 4. СБОРКА РАЗМЕТКИ ДЛЯ WinOLS ############"
# shellcheck disable=SC2086
python3 tools/report.py --profile "$PROFILE" $XFER_ARG \
    --maps out/maps_stock.json --grids out/grids_stock.json \
    --a2l out/FBH3ID60.a2l --csv out/FBH3ID60.csv \
    --md out/РАЗМЕТКА.md --json out/FBH3ID60_maps.json

echo
echo "############ 5. ЧТО ИЗМЕНИЛ КАЛИБРОВЩИК ############"
echo "--- сток -> csok ---"
python3 tools/whatchanged.py --maps out/FBH3ID60_maps.json "$STOCK" "$CSOK1" \
    --md "out/ЧТО_ИЗМЕНЕНО.md" | tail -n +3
echo
echo "--- csok -> csok v2 ---"
python3 tools/whatchanged.py --maps out/FBH3ID60_maps.json "$CSOK1" "$CSOK2" \
    --md "out/ЧТО_ИЗМЕНЕНО_csok1_csok2.md" | tail -n +3

echo
echo "############ 6. КАРТЫ ЗАЖИГАНИЯ ############"
for m in KFZWOP KFZW KFZW2 KFZWMS KFZWMN; do
  python3 tools/showmap.py --profile "$PROFILE" --name "$m" \
      "$STOCK" "$CSOK1" "$CSOK2" > "out/${m}.txt" 2>/dev/null \
      && echo "  $m -> out/${m}.txt" || true
done

echo
echo "Готово. Главное:"
echo "  out/ЧТО_ИЗМЕНЕНО.md -- что сделал калибровщик, с именами и в градусах"
echo "  out/РАЗМЕТКА.md     -- все найденные карты"
echo "  out/FBH3ID60.a2l    -- импортировать в WinOLS (с масштабами)"
echo "  out/FBH3ID60.csv    -- та же таблица для Excel"
