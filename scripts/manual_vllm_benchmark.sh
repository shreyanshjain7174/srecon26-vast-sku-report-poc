#!/usr/bin/env bash
set -euo pipefail

endpoint="${BENCH_ENDPOINT:-http://127.0.0.1:28000}"
model="${BENCH_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
out="${BENCH_OUTPUT_DIR:-/var/tmp/srecon26-measured-benchmark}"
mkdir -p "$out"

request_once() {
  local arm="$1" number="$2" prompt="$3" max_tokens="$4"
  local body="$out/${arm}-request-${number}-body.ndjson"
  local timing="$out/${arm}-request-${number}-curl.json"
  local result="$out/${arm}-request-${number}.json"
  local payload usage response_model
  payload="$(jq -cn --arg model "$model" --arg prompt "$prompt" --argjson max_tokens "$max_tokens" \
    '{model:$model,prompt:$prompt,max_tokens:$max_tokens,temperature:0,stream:true,stream_options:{include_usage:true}}')"
  curl --silent --show-error --fail --max-time 300 \
    -H 'Content-Type: application/json' -d "$payload" \
    -w '{"ttft_seconds":%{time_starttransfer},"e2e_seconds":%{time_total},"http_code":%{http_code}}\n' \
    -o "$body" "$endpoint/v1/completions" >"$timing"
  usage="$(sed -n 's/^data: //p' "$body" | grep -v '^\[DONE\]$' | jq -sc '[.[] | select(type=="object" and .usage!=null)] | last.usage')"
  response_model="$(sed -n 's/^data: //p' "$body" | grep -v '^\[DONE\]$' | jq -sr '[.[] | select(type=="object" and .model!=null)] | first.model')"
  jq -n --slurpfile timing "$timing" --arg arm "$arm" --argjson number "$number" \
    --arg requested_model "$model" --arg response_model "$response_model" --argjson usage "$usage" \
    '($timing[0] + {arm:$arm,request_number:$number,requested_model:$requested_model,response_model:$response_model,usage:$usage})
     | .prompt_tokens=$usage.prompt_tokens | .completion_tokens=$usage.completion_tokens | .total_tokens=$usage.total_tokens
     | .tpot_seconds=(if .completion_tokens>=2 then ((.e2e_seconds-.ttft_seconds)/(.completion_tokens-1)) else null end)
     | .generation_tokens_per_second=(if .completion_tokens>=2 then ((.completion_tokens-1)/(.e2e_seconds-.ttft_seconds)) else null end)' >"$result"
}

run_arm() {
  local arm="$1" concurrency="$2" repetitions="$3" max_tokens="$4"
  local prompt="Explain why queue depth can rise before CPU saturation in an LLM server."
  local i sample
  for ((i=0; i<repetitions; i++)); do prompt+=" pressure"; done
  curl --silent --show-error --fail "$endpoint/metrics" >"$out/${arm}-metrics-before.txt"
  nvidia-smi --query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu,power.draw --format=csv,noheader,nounits >"$out/${arm}-gpu-before.csv"
  local -a pids=()
  for ((i=1; i<=concurrency; i++)); do
    request_once "$arm" "$i" "$prompt" "$max_tokens" &
    pids+=("$!")
  done
  : >"$out/${arm}-metrics-during.ndjson"
  for ((sample=1; sample<=40; sample++)); do
    curl --silent --show-error --fail "$endpoint/metrics" \
      | awk -v observed_at="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)" -v sample="$sample" \
        '/^(vllm:num_requests_waiting|vllm:num_requests_running|vllm:kv_cache_usage_perc|vllm:gpu_cache_usage_perc)/ {print "{\"observed_at\":\"" observed_at "\",\"sample\":" sample ",\"metric\":\"" $1 "\",\"value\":" $2 "}"}' \
      >>"$out/${arm}-metrics-during.ndjson"
    sleep 0.1
  done
  local failed=0
  for i in "${pids[@]}"; do wait "$i" || failed=1; done
  [[ "$failed" == 0 ]]
  curl --silent --show-error --fail "$endpoint/metrics" >"$out/${arm}-metrics-after.txt"
  nvidia-smi --query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu,power.draw --format=csv,noheader,nounits >"$out/${arm}-gpu-after.csv"
  jq -s --arg arm "$arm" --argjson concurrency "$concurrency" \
    '[.[] | select(.arm == $arm)] | {arm:$arm,concurrency:$concurrency,requests:length,results:sort_by(.request_number)}' \
    "$out/${arm}"-request-*.json >"$out/${arm}-summary.json"
}

curl --silent --show-error --fail "$endpoint/health" >/dev/null
curl --silent --show-error --fail "$endpoint/v1/models" >"$out/models.json"
request_once warmup 1 "Reply with the word ready." 16
run_arm c4 4 256 128
run_arm c32 32 256 128
jq -s '{schema:"srecon26-vllm-benchmark/v1",arms:.}' "$out/c4-summary.json" "$out/c32-summary.json" >"$out/benchmark.json"
sha256sum "$out"/* >"$out/SHA256SUMS"
