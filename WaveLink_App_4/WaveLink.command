#!/bin/zsh
cd "$(dirname "$0")"
source /path/to/radioconda/bin/activate
python3 -m wavelink run --id 2 --name Pasan --uri ip:192.168.1.10 --key "WaveLink"

# Wait in the background until localhost:8080 is live, then open browser
(
  while ! curl -s http://localhost:8080/ > /dev/null; do
    sleep 0.5
  done
  xdg-open http://localhost:8080/ 2>/dev/null || open http://localhost:8080/
) &