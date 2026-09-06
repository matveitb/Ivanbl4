#!/usr/bin/env bash
# Полный прогон анализа прошивок Kia Spectra 1.6 / Bosch M7.9.7 (FBH3ID60).
#
#   ./analyze.sh                       # взять файлы из firmware/ по умолчанию
#   ./analyze.sh сток.bin csok1.bin csok2.bin
#
# Результаты -- в out/.

set -eu
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/tools:${PYTHONPATH:-}"
mkdir -p out

STOCK="${1:-firmware/FBH3ID60_stok.bin}"
CSOK1="${2:-firmware/FBH3ID60 e2 tun csok.bin}"
CSOK2="${3:-firmware/FBH3ID60 e2 tun csok v2___.bin}"

PROFILE="profiles/FBH3ID60.json"
# Калибровочная зона: код начинается с 0x10000, дальше карты вперемешку с кодом.
CAL_START=0x10000
CAL_END=0x1C000

for f in "$STOCK" "$CSOK1" "$CSOK2"; do
  [ -f "$f" ] || { echo "Нет файла: $f" >&2; exit 2; }
done

echo "############ 1. ОБЗОР ОБРАЗОВ ############"
for f in "$STOCK" "$CSOK1" "$CSOK2"; do
  t="out/$(basename "${f%.*}")_info.txt"
  python3 tools/fwinfo.py "$f" > "$t"
  echo "  $(basename "$f") -> $t"
done
sed -n '1,8p' "out/$(basename "${STOCK%.*}")_info.txt"

echo
echo "############ 2. КОНТРОЛЬНЫЕ СУММЫ ############"
for f in "$STOCK" "$CSOK1" "$CSOK2"; do
  printf '  %-40s ' "$(basename "$f")"
  python3 tools/bosch_csum.py check "$f" | tail -1
done

echo
echo "############ 3. ДИФФ csok1 vs csok2 (правки зажигания) ############"
python3 tools/fwdiff.py "$CSOK1" "$CSOK2" --identify \
    --out out/diff_csok1_csok2.json | tee out/diff_csok1_csok2.txt

echo
echo "############ 4. ДИФФ сток vs csok1 (весь тюнинг) ############"
python3 tools/fwdiff.py "$STOCK" "$CSOK1" --identify \
    --out out/diff_stock_csok1.json | tee out/diff_stock_csok1.txt

echo
echo "############ 5. КАРТЫ С ЗАГОЛОВКОМ (формат Bosch) ############"
python3 tools/mapscan.py "$STOCK" --range $CAL_START $CAL_END \
    --min-smooth 0.6 --min-cells 20 --out out/maps_stock.json --top 25 \
    > out/maps_stock.txt 2>&1
tail -30 out/maps_stock.txt

echo
echo "############ 6. ГОЛЫЕ ТАБЛИЦЫ (без заголовка) ############"
python3 tools/gridscan.py "$STOCK" --range $CAL_START $CAL_END \
    --out out/grids_stock.json > out/grids_stock.txt 2>&1
head -20 out/grids_stock.txt

echo
echo "############ 7. ОСИ (цепочечный разбор) ############"
python3 tools/axisscan.py "$STOCK" --range $CAL_START $CAL_END \
    --chains --min-links 4 --out out/axes_stock.json > out/axes_stock.txt 2>&1
grep -E '^---' out/axes_stock.txt || true

echo
echo "############ 8. СБОРКА РАЗМЕТКИ ДЛЯ WinOLS ############"
python3 tools/report.py --profile "$PROFILE" \
    --maps out/maps_stock.json --grids out/grids_stock.json \
    --a2l out/FBH3ID60.a2l --csv out/FBH3ID60.csv \
    --md out/РАЗМЕТКА.md --json out/FBH3ID60_maps.json

echo
echo "############ 9. КАРТЫ ЗАЖИГАНИЯ: СТОК vs ТЮНИНГ ############"
for m in IGN_MAIN_11x16 IGN_2_12x15 IGN_3_12x16; do
  python3 tools/showmap.py --profile "$PROFILE" --name "$m" \
      "$STOCK" "$CSOK1" "$CSOK2" > "out/${m}.txt"
  echo "  $m -> out/${m}.txt"
done

echo
echo "Готово. Главное:"
echo "  out/РАЗМЕТКА.md     -- отчёт по всем найденным картам"
echo "  out/FBH3ID60.a2l    -- импортировать в WinOLS"
echo "  out/FBH3ID60.csv    -- та же таблица для Excel"
echo "  out/IGN_*.txt       -- карты зажигания со сравнением"
