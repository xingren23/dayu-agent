#!/bin/zsh
setopt MONITOR  # 非交互脚本默认不启用 job 监控，显式开启以确保 ${#jobstates} 可用
set -uo pipefail

MAX_JOBS=4
log_dir="workspace/tmp/process_logs"
mkdir -p "$log_dir"

total_start_ts=$(date +%s)
failed_tickers=0
typeset -a child_pids=()

tickers_csv=$(ls -1 workspace/portfolio/ | tr '\n' ',' | sed 's/,$//')
tickers=(${(s:,:)tickers_csv})
sum=${#tickers[@]}

if (( sum == 0 )); then
  echo "[process] tickers_csv 为空"
  exit 1
fi

i=0
for ticker in "${tickers[@]}"; do
  while (( ${#jobstates} >= MAX_JOBS )); do
    sleep 0.5
  done

  ((i++))
  idx=$i

  (
    log_file="${log_dir}/${ticker}_process.log"
    ticker_start_ts=$(date +%s)

    echo "[processing][$idx/$sum][$ticker]"

    if python -m dayu.cli process --ci --ticker "$ticker" "$@" > "$log_file" 2>&1; then
      exit_code=0
    else
      exit_code=$?
      echo "[process][$idx/$sum][$ticker] 失败，exit=${exit_code}" >> "$log_file"
    fi

    ticker_end_ts=$(date +%s)
    ticker_cost=$((ticker_end_ts - ticker_start_ts))
    echo "[process][$idx/$sum][$ticker] 耗时: ${ticker_cost}s, exit=${exit_code}, log=${log_file}"
    exit ${exit_code}
  ) &
  child_pids+=($!)
done

for pid in "${child_pids[@]}"; do
  if ! wait "${pid}"; then
    ((failed_tickers+=1))
  fi
done

total_end_ts=$(date +%s)
total_cost=$((total_end_ts - total_start_ts))
echo "[process] 总耗时: ${total_cost}s, failed_tickers=${failed_tickers}"

if (( failed_tickers > 0 )); then
  exit 1
fi
