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
| Digital modulation | DS-CDMA with 31-chip Gold codes, RRC α = 0.35. BPSK preamble/header; payload **BPSK, QPSK or 16-QAM**, chosen per link from the receiver's measured SNR (adaptive modulation) |
| Two transceivers, real two-way link | ANTSDR E200 on each PC; full-duplex CDMA; ACKs flow on the reverse link |
| Packetization and reassembly | 1,010-byte fragments (`fast`), up to 65,535 per message (≈ 66 MB) — `mac.py` |
| Unique addressing | Node ID 1–30 = its Gold code (PHY) + `dst` byte checked in every frame (MAC) |
| CRC error detection | CRC-8 on the PHY length header; CRC-32 on every MAC frame — bad frames are dropped |
| ACK-based reliable delivery | Pipelined selective-repeat ARQ: cumulative + selective (SACK) ACKs, fast loss detection, timeout + random back-off, duplicate detection |
| Payloads: text, images, audio | Text; photos (compressed in the browser to ≤ 640 px WebP/JPEG); voice notes (Opus 16 kb/s) or audio files; plus any file |
| Application | Chat with 1:1 chats, an "Everyone" group, delivery and read ticks, and a tic-tac-toe game over the link |
| **Bonus**: encryption | `--key <passphrase>` → AES-256-GCM, scrypt KDF, addresses authenticated as AAD |
| **Bonus**: priority handling | 4 classes (control/ACK > urgent ⚡ > normal > bulk media); urgent text pre-empts an image between windows |
| **Bonus**: graphical dashboard | Radio panel: constellation, spectrum, Eb/N0, chip SNR, CFO, counters, queues, links |
| Other additions | Adaptive modulation with SNR feedback in ACKs; 10 MB+ file transfer; multi-code transmission to several stations at once; spread-ALOHA reception of colliding same-code bursts; presence beacons; channel simulator; GNU Radio Companion flowgraphs |

## 2. Physical layer

| Parameter | `fast` profile (default) | `robust` profile |
|---|---|---|
| Carrier | 2.500 GHz (`--freq`) | same |
| Sample rate / RF bandwidth | 4 MS/s / 4 MHz | same |
| Chip rate | 2 Mchip/s (2 samples/chip) | 1 Mchip/s (4 samples/chip) |
| Spreading | 31-chip Gold codes (33 codes), processing gain 14.9 dB | same |
| Payload bit rate BPSK / QPSK / 16-QAM | **64.5 / 129 / 258 kb/s** | 32 / 65 / 129 kb/s |
| Max frame payload | 1,024 bytes | 512 bytes |
| Pulse shape | Root-raised cosine, α = 0.35, 8-chip span | same |

Every station must use the same profile (`--profile`). `fast` uses the same CPU as `robust` and, thanks to
fractional-delay timing in the receiver, has the same sensitivity, so it is the default. `robust` halves the occupied
bandwidth.

**Burst:** 48 known preamble symbols (BPSK, code-specific) · 24-bit header (BPSK: 14-bit length, 2-bit
modulation, CRC-8) · payload in BPSK (differentially coded), QPSK or 16-QAM (Gray-mapped, coherent). The receiver
reads the modulation from each header, so it can change on every frame.

**Adaptive modulation.** Each ACK carries the SNR the receiver measured on the frame that requested it. The sender
picks 16-QAM if that SNR is at least 23 dB, QPSK if at least 15 dB, and BPSK otherwise. It drops a level for 10 s
whenever a window loses more than 20 % of its frames. Interference counts too: when another link shares the channel
at equal power, the measured SNR falls to about 15 dB, and the link steps down from 16-QAM to QPSK by itself.
`--mod bpsk|qpsk|16qam` forces one.

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
5. **Fractional-delay despreading.** Each symbol is despread at a few whole-sample offsets, and an 8-tap
   windowed-sinc filter interpolates between them to 1/64-sample timing resolution. That accuracy is what 16-QAM
   needs; without it, inter-chip interference caps the SNR near 12 dB. An early–late **DLL** tracks the
   sample-clock offset between the two radios, up to 30 ppm.
6. **Carrier tracking.** BPSK uses a block-wise squaring estimate plus differential decoding. QPSK and 16-QAM use a
   decision-directed block tracker (a 2nd-order loop) anchored on the preamble, so there is no 90° ambiguity to
   slip into.
7. Slicing (Gray-coded), CRC-8 header check, then the CRC-32 check at the link layer.

Spurious detections are rejected three ways: a longer preamble (48 bits) with a 0.8 threshold, code-specific preamble
patterns, and a "ghost" filter. The ghost filter rejects partial-correlation sidelobes of a much stronger burst and
any duplicate decodes; this matters at the high SNR of a short lab link.

### Measured performance (simulation: `tests.test_modem`, `tests.test_profiles`, `tests.test_bigfile`)

| Test | Result |
|---|---|
| BPSK, 200-byte frames, CFO ±12 kHz, 20 ppm clock offset | 12/12 at Eb/N0 10 dB (**chip SNR −4.9 dB**, below the noise) |
| Symbol SNR needed for 1 KB frames (both profiles) | BPSK and QPSK about 12–14 dB; 16-QAM about 20 dB |
| Implementation SNR ceiling (receiver accuracy) | about 44 dB for every modulation |
| 2 s of pure noise on two codes | 0 detections, 0 frames |
| Two simultaneous links, interferer +10 dB (BPSK) | both decoded (near-far tolerance) |
| Two senders → same receiver, overlapping | 20/20 bursts decoded |
| Decode cost per 1 KB frame | 10–27 ms; one receiver keeps up with continuous maximum-rate traffic (1.1–1.6× real time on a 2-core VM) |
| **2 MB file, `fast`, SNR 28 dB, adaptive** | **68 s, 235 kb/s goodput, 0 retransmissions** (started on QPSK, then 16-QAM) |
| **10 MB file, same conditions** | **340 s (5 min 40 s), 235 kb/s goodput, intact, 0 retransmissions** (9,893 of 9,901 frames on 16-QAM) |

## 3. Link layer (MAC)

```
DATA   | type | flags | dst | src | msg_id(2) | frag_idx(2) | frag_count(2) | payload ≤1010             | CRC-32 |
ACK    | type | flags | dst | src | msg_id(2) | frag_count(2) | base(2) | trigger(2) | snr(1) | SACK   | CRC-32 |
BEACON | type | flags | 255 | src | 0         | {"n": name, "e": key-id}                              | CRC-32 |
flags: bits 0–1 priority, bit 2 ACK-request
```

- **Addressing.** Frames whose `dst` is neither me nor 255 are dropped and never ACKed. Frames whose `src` is me
  are dropped too, since a station hears its own transmitter.
- **Reliability: pipelined selective-repeat ARQ.**
  - The sender streams fragments and sets ACK-request on every *W*-th frame, with *W* chosen so a window is about
    0.5 s on air (≈ 16 frames at 16-QAM).
  - After each ACK request it pauses briefly (the *ACK gap*) until the ACK arrives. That lets the receiver reply
    without transmitting over incoming frames, which its own transmitter would swamp.
  - The ACK says: everything below fragment `base` is received; `SACK` is a bitmap of the next 1,024 fragments;
    `trigger` is the fragment that requested this ACK; `snr` is the measured link quality.
  - Any fragment sent *before* the trigger frame and not acknowledged is known lost and is resent immediately,
    with no timeout needed.
  - If no ACK arrives within 0.35 s (simulation) or 0.5 s (hardware), everything in flight is resent after a random
    back-off. The message fails after 30 failures in a row with no progress.
  - If the ACK-requesting frame is lost, the receiver ACKs anyway after a quiet gap. Duplicates are re-ACKed but
    delivered only once.
  - A text message is one fragment, so it behaves like stop-and-wait.
- **Priority.** Messages to the same station are served in priority order, and at most about one window of airtime
  is queued per code. An urgent text therefore gets in front of a 10 MB transfer within about 0.5 s. ACKs always go
  first.
- **Presence.** A beacon every 5 s (±20 %) on the broadcast code. A contact goes offline after 3.5 missed beacons.

## 4. Application layer

Each message envelope is `crypto byte | meta length | meta JSON | body`. The meta JSON holds the kind, uid,
timestamp, group, filename and mime type.

- **Kinds:** `text`, `image`, `audio`, `file`, `game`, and `rcpt` (read receipt). Text, and files that compress
  (documents, CSV, logs), are zlib-compressed before sending. Already-compressed data is sent as it is.
- **Large files:** up to 64 MB per message. The UI shows an airtime estimate at the link's current modulation, a
  progress bar with retries on the sender's bubble, and "receiving n/N fragments" on the receiver.
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
| `--profile` | `fast` | `fast` (2 Mchip/s, up to 258 kb/s) or `robust` (1 Mchip/s). Every station must match. |
| `--mod` | `auto` | Adaptive BPSK/QPSK/16-QAM, or force `bpsk`, `qpsk` or `16qam` |
| `--freq`, `--rate` | 2.5 GHz, 4 MS/s | Every station must match |
| `--gold` | 5 | 6 or 7 for 63/127-chip codes: more processing gain, lower rate |
| `--ack-gap` | 0.15 s | Maximum pause after an ACK request, so the receiver can answer while the sender is silent. It must cover the radio's TX + RX latency. Raise it if you see many ACK timeouts. |
| `--hd-defer` | 0.25 s | Holds new transmissions while a burst for us is arriving, to limit self-interference |
| `--host 0.0.0.0` | — | Lets other PCs view the UI. The microphone only works on `localhost`. |

### 5.4 No hardware: the simulator

```bash
python3 -m cdmachat sim --nodes 4                 # Alice..Dave on http://localhost:8081 … 8084
python3 -m cdmachat sim --nodes 3 --key secret --ebn0 12
python3 -m cdmachat sim --nodes 2 --mod 16qam       # force a modulation
```

Every link gets its own gain (±3 dB), delay, carrier offset (±2 ppm LOs at 2.5 GHz) and AWGN. The default SNR is
25 dB, so links run QPSK or 16-QAM. The SNR slider in the dashboard lets you degrade the channel live and watch
the modulation step down (16-QAM → QPSK → BPSK), CRC drops and retransmissions. The simulator is CPU-bound: with
several stations busy it runs slower than real time, but its clock is simulated, so throughput figures stay
correct.

### 5.5 GNU Radio Companion flowgraphs (`grc/`)

| File | What it is |
|---|---|
| `cdma_station.grc` | One complete station on an ANTSDR E200 with the Pluto/libiio firmware. Includes RX-gain and TX-attenuation sliders, the received spectrum and waterfall, and a transmit scope. |
| `cdma_station_uhd.grc` | The same station for the E200's UHD firmware (`type=ant`). |
| `cdma_two_nodes_sim.grc` | Two full stations (Alice :8081, Bob :8082) over a simulated channel, with noise and carrier-offset sliders. No hardware needed. |
| `cdma_phy_concept.grc` | DS-CDMA built only from stock GNU Radio blocks. Two users with Gold codes #1 and #2 are summed with noise, then despread with the right and the wrong code. Use it for the report and viva: it shows spreading, despreading and processing gain visually. |

Open a file in `gnuradio-companion` and press **Run (F6)**, then open the web UI at the port shown in the console.
The station graphs can also be run from a terminal after generating them:
`python3 grc/cdma_station.py -i 2 -n Bob -u ip:192.168.1.10`.

- The two **CDMA Chat TX / RX** boxes are Embedded Python Blocks. When the flowgraph starts they launch the station
  (link layer, chat app, web UI) from the `cdmachat` package, so **keep the `grc/` folder inside the project folder**.
- Give each station a different **Node ID** in the Parameter blocks. The **Speed profile** (`fast`/`robust`) must
  match on all stations; **Modulation** is `auto` or a forced `bpsk`/`qpsk`/`16qam`.
- The `.grc` files are generated by `tools/make_grc.py`. Edit that script and rerun it, rather than hand-editing
  the files, if you want to change the shipped versions.
- All four files were checked with GNU Radio 3.10's own GRC loader and code generator: they validate and generate
  compilable Python. The generated station blocks were also run end to end through a simulated channel. They have
  not yet been opened in a GRC GUI on a lab PC.

## 6. Demo plan for the final evaluation (≈ 10 min)

1. **Bring-up.** Start four stations. Show presence and the PHY line (2.5 GHz, 31-chip Gold, BPSK/QPSK/16-QAM, my
   code).
2. **Text plus ACK.** Alice → Bob. Ticks go 🕒 → ✓ → ✓✓, then blue when Bob opens the chat. Point to Eb/N0 and CFO
   in the chat header.
3. **Addressing.** Carol's dashboard shows no frames for the Alice→Bob traffic, because it is on code #2. Run
   `tx --to 2` from a PC while Carol runs `rx --id 3`: nothing is printed.
4. **CDMA concurrency.**
   - Alice sends a photo to Bob while Carol sends a voice note to Dave, and Dave texts Alice, all at once on one
     carrier.
   - The dashboard shows several active TX codes.
   - Goodput shows on each bubble.
5. **CRC plus ARQ.** Move an antenna away, or raise `--tx-atten`. The dashboard shows CRC-32 drops and
   retransmissions while the image still arrives intact.
6. **Priority.** Start a large image, then send an ⚡ urgent text. It arrives first.
7. **Encryption.** Restart one station with a different `--key`. The UI warns, and its messages can't be opened.
8. **Application.** Play a game of tic-tac-toe across the room.
9. **Adaptive modulation and big files.**
   - Send a multi-MB file Alice → Bob. The chat header shows "sending 16-QAM (258 kb/s)", and the dashboard
     constellation shows 16 clusters.
   - Start a second transfer Carol → Dave on another code. Alice→Bob steps down to QPSK, because the reported SNR
     now includes the other link's interference.

## 7. Tests

```bash
python3 -m tests.test_modem        # PHY: impairments, false alarms, multi-user, collisions, Eb/N0 sweep, CPU
python3 -m tests.test_profiles     # BPSK/QPSK/16-QAM per profile: SNR sweep, 2nd link, decode cost, real time
python3 -m tests.test_bigfile 10 fast 28 auto   # 10 MB transfer over the simulated air
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
- **High Eb/N0 but many "CRC-32 drops" and retransmissions.** The radio link is fine; samples are being lost.
  Open the Counters panel. If **TX underruns** is climbing, the transmitter's buffer ran dry mid-burst, so frames
  went out with a hole in them. Raise `--tx-buffer 262144` (66 ms) and close heavy programs. If
  **RX overflows** is climbing, the PC can't keep up with receiving; see the next items.
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

- **No FEC.** The link relies on processing gain plus ARQ. A rate-½ convolutional or LDPC code would buy about
  5 dB, enough to run 16-QAM about 5 dB further out.
- **16-QAM needs a quiet channel.** 31-chip spreading suppresses another equal-power link by about 15 dB, while
  16-QAM needs about 20 dB. Adaptive modulation copes by stepping down to QPSK when the channel is busy.
- **Throughput ceiling.** About 235 kb/s goodput per link at 4 MS/s, limited by Python DSP CPU. A C++ OOT
  correlator would allow higher chip rates, and 64-QAM would need SNRs above 26 dB.
- **Near-far.** There is no power control; a station much closer than the others can swamp weaker links on other
  codes. Beacons could drive closed-loop TX power control from the measured Eb/N0.
- **Single path.** There is no RAKE receiver or equalizer. At 0.5 µs chips, indoor multipath (delay spreads of
  tens of ns) mostly stays within one chip. A 2–3 finger RAKE would help in large rooms, and matters most for
  16-QAM.
- **Spreading is fixed.** Adaptive modulation trades rate for range already; adaptive spreading (31/63/127 by
  measured SNR) would extend range further.
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
  grc_glue.py    connects the GRC Embedded Python Blocks to a station
  __main__.py    CLI: run | sim | tx | rx | selftest
grc/             GNU Radio Companion flowgraphs (station iio/UHD, 2-node sim, PHY concept)
tools/make_grc.py  regenerates the .grc files
tests/           modem, link, GR-backend (fake GR), TX-underrun, API scenario
```
