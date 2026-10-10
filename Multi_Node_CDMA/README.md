# CDMA Chat — multi-node DS-CDMA messaging over ANTSDR E200 + GNU Radio

EN2130 Communication Design Project (2026). A WhatsApp-style chat for 2–4 stations. Every station is one PC with
one ANTSDR E200. All stations share **2.5 GHz** using **direct-sequence CDMA with BPSK**, so several links can
be active at the same instant on the same carrier.

```
 Browser (localhost:8080)                       one station = one PC + one ANTSDR E200
 ┌───────────────────────────────┐
 │ chats · photos · voice notes  │  web/        stdlib HTTP + Server-Sent Events (no pip installs)
 │ files · tic-tac-toe · radio   │
 │ dashboard                     │
 └──────────────┬────────────────┘
                │ REST / SSE
 ┌──────────────┴────────────────┐
 │ Application    node.py        │  message envelope, AES-256-GCM, group fan-out, read receipts,
 │                               │  presence (beacons), history, media store
 ├───────────────────────────────┤
 │ Link layer     mac.py         │  addressing, CRC-32, fragmentation/reassembly, block-ACK
 │                               │  selective-repeat ARQ, priority scheduler, duplicate filter
 ├───────────────────────────────┤
 │ CDMA PHY       modem.py       │  Gold codes, RRC, preamble detector, CFO, PLL, DLL, CRC-8 header
 │                codes.py       │
 ├───────────────────────────────┤
 │ Radio          radio_gr.py    │  GNU Radio flowgraph: custom gr.sync_blocks ⇄ gr-iio / UHD (ANTSDR)
 │                radio_sim.py   │  or the built-in multi-node channel simulator (no hardware)
 └───────────────────────────────┘
```

## 1. Requirements checklist (from the project brief)

| Brief requirement | Where / how |
|---|---|
| **Mid-evaluation**: simplex text TX → RX | `python -m cdmachat tx …` and `python -m cdmachat rx …` (section 5.2) |
| Digital modulation | BPSK (differentially encoded), DS-spread with 31-chip Gold codes, RRC α = 0.35 |
| Two transceivers, real two-way link | ANTSDR E200 on each PC; full-duplex CDMA; ACKs flow on the reverse link |
| Packetization and reassembly | 241-byte fragments, up to 1,944 per message (~460 KB) — `mac.py` |
| Unique addressing | Node ID 1–30 = its Gold code (PHY) + `dst` byte checked in every frame (MAC) |
| CRC error detection | CRC-8 on the PHY length header; CRC-32 on every MAC frame — bad frames are dropped |
| ACK-based reliable delivery | Block ACK with a bitmap, selective repeat, timeout + random back-off, duplicate detection |
| Payloads: text, images, audio | Text; photos (compressed in the browser to ≤ 640 px WebP/JPEG); voice notes (Opus 16 kb/s) or audio files; plus any file |
| Application | Chat with 1:1 chats, an "Everyone" group, delivery and read ticks, and a tic-tac-toe game over the link |
| **Bonus**: encryption | `--key <passphrase>` → AES-256-GCM, scrypt KDF, addresses authenticated as AAD |
| **Bonus**: priority handling | 4 classes (control/ACK > urgent ⚡ > normal > bulk media); urgent text pre-empts an image between windows |
| **Bonus**: graphical dashboard | Radio panel: constellation, spectrum, Eb/N0, chip SNR, CFO, counters, queues, links |
| Other additions | Multi-code transmission to several stations at once; spread-ALOHA reception of colliding same-code bursts; presence beacons; channel simulator; 63/127-chip code option |

## 2. Physical layer

| Parameter | Value |
|---|---|
| Carrier | 2.500 GHz (`--freq`) |
| Sample rate / RF bandwidth | 4 MS/s / 4 MHz |
| Chip rate | 1 Mchip/s (4 samples/chip) |
| Spreading | Gold codes of length 31 (33 codes; `--gold 6/7` → 63/127 chips) |
| Bit rate | 32.26 kb/s per link (31 chips/bit) |
| Processing gain | 14.9 dB |
| Pulse shape | Root-raised cosine, α = 0.35, 8-chip span |
| Burst | 48-bit code-specific preamble · 16-bit length + CRC-8 · ≤ 255-byte payload |

**Code assignment (receiver-based CDMA).** Station *N* listens on Gold code #*N* and on the broadcast code #0. A frame
for station *N* is spread with code #*N*. Three consequences:

- A station never even despreads traffic meant for someone else.
- One transmitter can talk to several stations at the same instant by summing bursts on different codes
  (`TxMixer`). An image to Bob and a text to Carol go out together.
- Two senders to the same station collide on the same code. Because they arrive with different code phases, the
  receiver tracks both independently and decodes each, with the other acting as suppressed interference
  (spread-ALOHA reception).

**Receiver chain** (`modem.Demodulator`, pure numpy, shared by hardware and simulator):

1. RRC matched filter.
2. Code correlation at every sample offset.
3. **Differential preamble detector** `|Σ q_k c_k c*_{k-1}|`, normalised by the despread energy. It is immune
   to carrier offset, so it detects bursts even with ±16 kHz between two ANTSDR crystals. It also gives the symbol
   timing, and arg(·) gives a coarse CFO estimate.
4. CFO removal, then a linear fit of the preamble phase for the residual frequency.
5. Per-symbol despreading with a decision-directed 2nd-order **PLL** (carrier) and an early–late **DLL** (code
   timing; tracks the sample-clock offset between two radios).
6. Differential decoding, which removes the PLL's 180° ambiguity.
7. CRC-8 header check, then the CRC-32 check at the link layer.

Spurious detections are rejected three ways: a longer preamble (48 bits) with a 0.8 threshold, code-specific preamble
patterns, and a "ghost" filter. The ghost filter rejects partial-correlation sidelobes of a much stronger burst and
any duplicate decodes; this matters at the high SNR of a short lab link.

### Measured performance (simulation, `python -m tests.test_modem`)

| Test | Result |
|---|---|
| 200-byte frames, CFO ±12 kHz, 20 ppm clock offset | 12/12 at Eb/N0 = 10 dB (**chip SNR −4.9 dB**, i.e. below the noise) |
| Same conditions at Eb/N0 = 8 dB | 3–8 of 12 (theoretical knee for 1,600-bit frames) |
| 2 s of pure noise on two codes | 0 detections, 0 frames |
| Two simultaneous links, interferer +10 dB stronger | both decoded (near-far tolerance) |
| One TX → two RX at the same time (multi-code) | both decoded |
| Two senders → same RX, overlapping | 20/20 bursts decoded |
| RX CPU at 4 MS/s, 2 codes | 2.2–2.6× faster than real time on a 2-core VM |

Link layer over the simulated air (`python -m tests.test_link`): a 30 KB image (125 fragments) was delivered in
8.4 s, a **28.5 kb/s goodput** (88 % of the raw bit rate). During that transfer, texts in both directions were
delivered in about 0.1 s on other codes. An urgent message queued behind a 20 KB transfer is delivered first.

## 3. Link layer (MAC)

```
DATA   | type | flags | dst | src | msg_id(2) | frag_idx(2) | frag_count(2) | payload ≤241 | CRC-32 |
ACK    | type | flags | dst | src | msg_id(2) | frag_count(2) | bitmap (1 bit per fragment)  | CRC-32 |
BEACON | type | flags | 255 | src | 0         | {"n": name, "e": key-id}                     | CRC-32 |
flags: bits 0–1 priority, bit 2 ACK-request
```

- **Addressing.** Frames whose `dst` is neither me nor 255 are dropped and never ACKed. Frames whose `src` is me
  are dropped too, since a station hears its own transmitter.
- **Reliability.**
  - The sender transmits a window of up to 8 missing fragments; the last one requests an ACK.
  - The receiver answers with a bitmap ACK, which jumps to the front of its TX queue.
  - The sender resends only the gaps. On a timeout (0.35 s sim / 0.5 s hardware + ACK airtime) it backs off by a
    random time that grows with each failure, up to 15 retries.
  - If the ACK-requesting frame is lost, the receiver ACKs anyway after a quiet gap.
  - Duplicates of finished messages are re-ACKed but not re-delivered.
- **Priority.** For each destination, the highest-priority ready message gets the next window, so an urgent text
  overtakes an image transfer within about 0.5 s. ACKs always go first.
- **Presence.** A beacon every 5 s (±20 %) on the broadcast code. A contact goes offline after 3.5 missed beacons.

## 4. Application layer

Each message envelope is `crypto byte | meta length | meta JSON | body`. The meta JSON holds the kind, uid,
timestamp, group, filename and mime type.

- **Kinds:** `text` (zlib-compressed when that helps), `image`, `audio`, `file`, `game`, and `rcpt` (read receipt).
- **Group "Everyone":** the message is sent as a separate reliable unicast to every online station, each on its own
  code and all in parallel. Per-recipient ticks show in the tooltip.
- **Ticks:**

  | Tick | Meaning |
  |---|---|
  | 🕒 | queued |
  | ✓ | first window on air |
  | ✓✓ | ACKed |
  | blue ✓✓ | the recipient opened the chat (read receipt) |
  | ! | failed after retries |

- **Encryption:** AES-256-GCM, with the key = scrypt(passphrase). Each message uses a random 96-bit nonce, and
  `(src, dst)` is the associated data. The UI shows a short key-ID, and warns when a peer's beacon advertises a
  different key.
- **History:** stored in `data/node<N>/history.jsonl`, with media files in `data/node<N>/media/`.

## 5. Running it

### 5.0 Install (each lab PC, Ubuntu 22.04/24.04)

```bash
sudo apt install gnuradio python3-numpy          # GNU Radio 3.10+ (gr-iio is built in from 3.10)
python3 -c "from gnuradio import iio; print('gr-iio ok')"
pip install cryptography                         # only needed for --key (encryption)
```

There are no other dependencies: the web server is pure Python standard library, and the UI loads nothing from the
internet. Copy this folder to each PC.

### 5.1 ANTSDR E200 setup

- **Pluto-compatible firmware (recommended, `--radio iio`).** Set the boot DIP switch to QSPI. The default address
  is `ip:192.168.1.10`; set the PC's Ethernet port to `192.168.1.x`.
- **UHD firmware (`--radio uhd`).** Set the boot DIP switch to SD and install MicroPhase's `antsdr_uhd` driver. The
  device args are `type=ant,addr=192.168.1.10`.
- **Several ANTSDRs on one switch** need different IPs. In the device shell run
  `fw_setenv ipaddr_eth 192.168.1.11` and reboot. With one PC per ANTSDR on a direct cable, all can stay at `.10`.
- **Antennas.** Use the TX1 and RX1 ports with separate 2.4 GHz-band antennas, a few cm apart, or more.
- **Bring-up order:**
  1. Check the PC can reach the device: `iio_info -u ip:192.168.1.10`.
  2. Check the GNU Radio install: `python -m cdmachat selftest --radio loopback`.

### 5.2 Mid-evaluation: simplex text link

```bash
# PC 2 (receiver)
python3 -m cdmachat rx --id 2 --uri ip:192.168.1.10
# PC 1 (transmitter): repeats every second, spread with node 2's code
python3 -m cdmachat tx --id 1 --to 2 --text "Hello from group 7" --uri ip:192.168.1.10
```

The receiver prints each message with its Eb/N0, carrier offset and preamble metric.

### 5.3 Final system: one station per PC

```bash
python3 -m cdmachat run --id 1 --name Alice --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m cdmachat run --id 2 --name Bob   --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m cdmachat run --id 3 --name Carol --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m cdmachat run --id 4 --name Dave  --uri ip:192.168.1.10 --key "our-group-secret"
```

Then open **http://localhost:8080** on each PC. Stations appear within a second or two.

- Click a contact to chat; "Everyone" is the group.
- 📎 sends a photo or file. The mic button records a voice note of up to 15 s.
- ⚡ marks a message urgent. The # button starts tic-tac-toe.
- The bar-chart icon opens the radio dashboard.

Useful flags:

| Flag | Default | Notes |
|---|---|---|
| `--tx-atten` | 10 dB | 0 = full power |
| `--rx-gain` | 40 dB | Raise for distance; lower if the dashboard spectrum is clipping |
| `--gain-mode` | `manual` | Or `slow_attack` |
| `--freq`, `--rate` | 2.5 GHz, 4 MS/s | Every station must match |
| `--gold` | 5 | 6 or 7 for 63/127-chip codes: more processing gain, lower rate |
| `--hd-defer` | 0.25 s | Holds a new TX window while a burst for us is arriving, to limit self-interference |
| `--host 0.0.0.0` | — | Lets other PCs view the UI. The microphone only works on `localhost`. |

### 5.4 No hardware: the simulator

```bash
python3 -m cdmachat sim --nodes 4                 # Alice..Dave on http://localhost:8081 … 8084
python3 -m cdmachat sim --nodes 3 --key secret --ebn0 12
```

Every link gets its own gain (±3 dB), delay, carrier offset (±2 ppm LOs at 2.5 GHz) and AWGN. The Eb/N0 slider in
the dashboard lets you degrade the channel live and watch CRC drops and retransmissions.

## 6. Demo plan for the final evaluation (≈ 10 min)

1. **Bring-up.** Start four stations. Show presence and the PHY line (2.5 GHz, 31-chip Gold, 32 kb/s, my code).
2. **Text plus ACK.** Alice → Bob. Ticks go 🕒 → ✓ → ✓✓, then blue when Bob opens the chat. Point to Eb/N0 and CFO
   in the chat header.
3. **Addressing.** Carol's dashboard shows no frames for the Alice→Bob traffic, because it is on code #2. Run
   `tx --to 2` from a PC while Carol runs `rx --id 3`: nothing is printed.
4. **CDMA concurrency.**
   - Alice sends a photo to Bob while Carol sends a voice note to Dave, and Dave texts Alice, all at once on one
     carrier.
   - The dashboard shows several active TX codes.
   - Goodput shows on each bubble, about 28 kb/s.
5. **CRC plus ARQ.** Move an antenna away, or raise `--tx-atten`. The dashboard shows CRC-32 drops and
   retransmissions while the image still arrives intact.
6. **Priority.** Start a large image, then send an ⚡ urgent text. It arrives first.
7. **Encryption.** Restart one station with a different `--key`. The UI warns, and its messages can't be opened.
8. **Application.** Play a game of tic-tac-toe across the room.

## 7. Tests

```bash
python3 -m tests.test_modem        # PHY: impairments, false alarms, multi-user, collisions, Eb/N0 sweep, CPU
python3 -m tests.test_link         # MAC over simulated air: concurrency, priority, lossy link
python3 -m tests.test_gr_backend   # GNU Radio backend code path against a fake GR (API-shape test)
python3 -m cdmachat sim --nodes 3 & python3 -m tests.scenario_api   # full app scenario through the web API
```

On a lab PC with GNU Radio, `python -m cdmachat selftest --radio loopback` runs the real flowgraph. The `iio` and
`uhd` backends were written against the gr-iio 3.10 `fmcomms2_source/sink` and gr-uhd APIs and the ANTSDR docs.
They have not been run on a physical E200 yet, so expect to tune gains on the first lab session.

## 8. Troubleshooting

- **No stations appear.** Check `iio_info -u <uri>`, then check that every PC uses the same `--freq`, `--rate` and
  `--gold`. Open the dashboard spectrum: you should see a ~1.35 MHz-wide hump while another station transmits.
- **"header CRC-8 fails" climbing, few frames.** The SNR is too low. Raise `--rx-gain`, lower `--tx-atten`, or move
  closer. If the spectrum is flat-topped or clipped, lower `--rx-gain`.
- **`rx_overflow_chunks` / `rx_shed_chunks` in the counters.** The PC can't keep up. Close other programs, or run
  `--rate 2e6 --sps 2` on all stations. That keeps the same bit rate at half the CPU, but costs roughly 2–3 dB of
  sensitivity, because chip timing is then only resolved to half a chip. In simulation it decoded 4/8 frames at
  Eb/N0 11 dB, against 8/8 with the defaults.
- **Retransmissions while two stations talk to each other at once.** Your own transmitter deafens your receiver.
  Separate the TX/RX antennas, lower TX power, or raise `--hd-defer`.
- **The microphone doesn't work.** Use `http://localhost`; browsers block the microphone on plain-HTTP remote
  addresses. You can attach an audio file instead.

## 9. Limitations and improvements (for the report)

- **No FEC.** The link relies on processing gain plus ARQ. A rate-½ convolutional code with Viterbi decoding would
  buy about 5 dB.
- **Near-far.** There is no power control; a station much closer than the others can swamp weaker links on other
  codes. Beacons could drive closed-loop TX power control from the measured Eb/N0.
- **Single path.** There is no RAKE receiver. Indoor multipath at 1 µs chips is mostly absorbed in one chip, but
  a 2–3 finger RAKE would help.
- **Fixed rate.** Adaptive spreading (31/63/127 by measured SNR) would trade rate for range per link.
- **Shared key.** Encryption uses one pre-shared key. Per-pair X25519 keys in the beacons would give forward
  secrecy.
- **Python DSP.** The receiver is numpy inside a Python block. Porting the correlator to a C++ OOT block would allow
  ~10 MS/s.

## 10. Suggested split for a group of four

| Member | Owns |
|---|---|
| A | PHY: `codes.py`, `modem.py`, BER measurements, hardware gain tuning |
| B | GNU Radio and hardware: `radio_gr.py`, ANTSDR firmware/IPs, `tx`/`rx` mid-eval demo |
| C | Link layer: `mac.py`, ARQ/priority tests, the simulator |
| D | Application and UI: `node.py`, `web/`, encryption, dashboard, demo script |

## Project layout

```
cdmachat/
  config.py      PHY parameters, address → code mapping
  codes.py       Gold codes, preamble patterns, RRC taps
  modem.py       burst modulator + streaming multi-code demodulator
  radio_base.py  TxMixer (multi-code transmit), radio interface, dashboard data
  radio_gr.py    GNU Radio flowgraph: ANTSDR via gr-iio or UHD, loopback
  radio_sim.py   multi-node channel simulator
  mac.py         link layer
  crypto.py      AES-256-GCM
  node.py        application layer
  web/           server.py + static/ (index.html, app.js, style.css)
  __main__.py    CLI: run | sim | tx | rx | selftest
tests/           modem, link, GR-backend (fake GR), API scenario
```
