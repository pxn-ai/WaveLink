# CDMA Chat — multi-node DS-CDMA messaging over ANTSDR E200 + GNU Radio

EN2130 Communication Design Project (2026). A WhatsApp-style chat for 2–4 stations. Every station is one PC with
one ANTSDR E200. All stations share **2.5 GHz** using **direct-sequence CDMA** (31-chip Gold codes) with **BPSK,
QPSK or 16-QAM** payloads, so several links can be active at the same instant on the same carrier.

**The physical layer is a GNU Radio Companion flowgraph** (`grc/cdma_phy.grc`). When a station starts, the app
compiles it with `grcc`, runs it, and attaches its link layer and web UI to the flowgraph's two interface blocks.

```
 Browser (localhost:8080)                        one station = one PC + one ANTSDR E200
 ┌───────────────────────────────┐
 │ chats · photos · voice notes  │  web/        stdlib HTTP + Server-Sent Events
 │ files · tic-tac-toe · radio   │
 │ dashboard (constellation,     │
 │ spectrum, counters)           │
 └──────────────┬────────────────┘
 ┌──────────────┴────────────────┐
 │ Application    node.py        │  message envelope, AES-256-GCM, group fan-out, read receipts,
 │                               │  presence (beacons), history, media store
 ├───────────────────────────────┤
 │ Link layer     mac.py         │  addressing, fragmentation, pipelined selective-repeat ARQ,
 │                               │  adaptive modulation, priority, duplicate filter
 ├────────── MAC -> PHY ─────────┤  ← Embedded Python blocks in the .grc (grc_phy.py)
 │ Physical layer                │
 │  grc/cdma_phy.grc             │  GNU Radio flowgraph: CRC-32, framing, Gold-code spreading
 │  (compiled with grcc at start)│  lanes, RRC, ANTSDR (Pluto) sink/source, despreading,
 │                               │  constellation decoders, CRC check
 └───────────────────────────────┘
```

## 1. Requirements checklist (from the project brief)

| Brief requirement | Where / how |
|---|---|
| **Mid-evaluation**: simplex text TX → RX | `python -m cdmachat tx …` and `python -m cdmachat rx …` (section 5.2) |
| Digital modulation | DS-CDMA with 31-chip Gold codes, RRC α = 0.35. BPSK preamble/header; payload **BPSK, QPSK or 16-QAM**, chosen per link from the receiver's measured SNR (adaptive modulation) |
| GNU Radio based | The PHY is `grc/cdma_phy.grc`, built from stock GNU Radio blocks plus five Embedded Python blocks; the app compiles and runs it (section 5.5) |
| Two transceivers, real two-way link | ANTSDR E200 on each PC; full-duplex CDMA; ACKs flow on the reverse link |
| Packetization and reassembly | 1,010-byte fragments (`fast`), up to 65,535 per message (≈ 66 MB) — `mac.py` |
| Unique addressing | Node ID 1–4 = its Gold code (PHY: one code lane per station in the flowgraph) + `dst` byte checked in every frame (MAC) |
| CRC error detection | CRC-8 on the PHY header; CRC-32 on every frame (stock **Stream CRC32** blocks in the flowgraph) — bad frames are dropped |
| ACK-based reliable delivery | Pipelined selective-repeat ARQ: cumulative + selective (SACK) ACKs, fast loss detection, timeout + random back-off, duplicate detection |
| Payloads: text, images, audio | Text; photos (compressed in the browser to ≤ 640 px WebP/JPEG); voice notes (Opus 16 kb/s) or audio files; plus any file |
| Application | Chat with 1:1 chats, an "Everyone" group, delivery and read ticks, and a tic-tac-toe game over the link |
| **Bonus**: encryption | `--key <passphrase>` → AES-256-GCM, scrypt KDF, addresses authenticated as AAD |
| **Bonus**: priority handling | 4 classes (control/ACK > urgent ⚡ > normal > bulk media); urgent text pre-empts an image between windows |
| **Bonus**: graphical dashboard | Radio panel: constellation, spectrum, Eb/N0, chip SNR, CFO, counters, queues, links |
| Other additions | Adaptive modulation with SNR feedback in ACKs; 10 MB+ file transfer; multi-code transmission to several stations at once; spread-ALOHA reception of colliding same-code bursts; presence beacons; channel simulator; GNU Radio Companion flowgraphs |

## 2. Physical layer

| Parameter | `grc/cdma_phy.grc` (default PHY) | numpy PHY `robust` profile |
|---|---|---|
| Carrier | 2.500 GHz (`--freq`) | same |
| Sample rate / RF bandwidth | 4 MS/s / 4 MHz | same |
| Chip rate | 2 Mchip/s (2 samples/chip) | 1 Mchip/s (4 samples/chip) |
| Spreading | 31-chip Gold codes, processing gain 14.9 dB | same |
| Payload bit rate BPSK / QPSK / 16-QAM | **64.5 / 129 / 258 kb/s** | 32 / 65 / 129 kb/s |
| Max frame payload | 1,024 bytes (MAC frame + CRC-32) | 512 bytes |
| Pulse shape | Root-raised cosine, α = 0.35, 8-chip span | same |

The flowgraph's rates are the numpy PHY's `fast` profile, so a GRC station and a `--phy numpy --profile fast`
station are compatible on air, apart from BPSK payloads (see "Burst" below).

**Burst:** 48 known preamble symbols (BPSK, code-specific) · 24-bit header (differential BPSK: 14-bit length,
2-bit modulation, CRC-8) · payload in BPSK, QPSK or 16-QAM (Gray-mapped, coherent), padded to a multiple of 16
symbols. The receiver reads the modulation from each header, so it can change on every frame. (The numpy PHY sends
BPSK payloads differentially coded; the GRC PHY sends them coherently, as header modulation code 3, so its stock
Constellation Decoder can slice them.)

### 2.1 The flowgraph (`grc/cdma_phy.grc`)

When a station starts, `python3 -m cdmachat run` compiles `grc/cdma_phy.grc` with `grcc` into `build/grc/`
(every time, so edits saved in GRC take effect on the next start), imports the generated `top_block`, sets its
parameters (node ID, ANTSDR URI, carrier, gains) and runs it. The link layer talks to the flowgraph only through the
**MAC → PHY** and **PHY → MAC** blocks. Everything between them is the flowgraph.

```
TX  MAC -> PHY ─► PDU to Tagged Stream ─► Stream CRC32 (append) ─► Tagged Stream to PDU ─► CDMA PHY Framer
    ─► 5 code lanes (code 0 = broadcast, 1–4 = nodes), each:
         PDU Filter (code = k) ─► PDU to Tagged Stream ─► Chunks to Symbols (BPSK/QPSK/16-QAM table)
         ─► Repeat ×31 ─► Multiply ◄─ Vector Source (Gold code k) ─► Tagged Stream Multiply Length
    ─► CDMA Burst Combiner ─► Interpolating FIR (RRC, ×2) ─► Multiply Const ─► PlutoSDR Sink (ANTSDR TX1)

RX  PlutoSDR Source (ANTSDR RX1) ─► FFT Filter (RRC matched filter) ─► CDMA Despreader
    ─► 3 modulation branches (BPSK, QPSK, 16-QAM), each:
         PDU Filter (mod) ─► PDU to Tagged Stream ─► Constellation Decoder ─► Repack Bits (k → 8)
         ─► Stream CRC32 (check, bad frames dropped) ─► Tagged Stream to PDU
    ─► PHY -> MAC
```

| Block | Kind | Job |
|---|---|---|
| MAC → PHY, PHY → MAC | Embedded Python | Hand frames between the app and the flowgraph as PDUs (metadata: `code`, `mod`; on RX also SNR, CFO, constellation points) |
| CDMA PHY Framer | Embedded Python | Builds the burst as constellation **indices**: preamble, header, payload bits → symbol numbers in `sym_table` (0–1 BPSK, 2–5 QPSK, 6–21 16-QAM) |
| CDMA Burst Combiner | Embedded Python | Adds the 5 code lanes chip by chip (several stations served at once) and sends zeros between bursts, so the ANTSDR always has samples |
| CDMA Despreader | Embedded Python | Synchronisation: preamble correlation on this node's code and the broadcast code, CFO estimate, fractional-delay despreading with DLL timing, header decode, carrier tracking. Outputs the corrected payload symbols |
| Everything else | Stock GNU Radio | CRC-32, spreading (Repeat × Gold code), Chunks to Symbols, pulse shaping, ANTSDR sink/source, matched filter, **Constellation Decoder** with `variable_constellation` objects, bit packing |

Modulation switching: the link layer chooses BPSK, QPSK or 16-QAM for each frame (adaptive, or forced with
`--mod`) and puts it in the PDU's `mod` field. The Framer maps it to the right part of the symbol table and writes
it into the header. On receive, the Despreader reads the header and tags the PDU with its modulation, and a PDU
Filter routes it to the matching Constellation Decoder.

The Embedded Python blocks keep their DSP in `cdmachat/grc_phy.py` and `cdmachat/modem.py`, so the code inside the
.grc stays short. Their source lives in `tools/epy/`; `tools/make_grc.py` writes it into the .grc files.

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

**Receiver synchronisation** (`modem.Demodulator`, numpy; inside the CDMA Despreader block on the GRC PHY, and
the whole receiver on the numpy PHY and in the simulator):

1. RRC matched filter (on the GRC PHY this is the stock FFT Filter in front of the Despreader).
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
7. CRC-8 header check, then slicing (Gray-coded) and the CRC-32 check. On the GRC PHY these last two are the
   stock Constellation Decoder and Stream CRC32 blocks.

Spurious detections are rejected three ways: a longer preamble (48 bits) with a 0.8 threshold, code-specific preamble
patterns, and a "ghost" filter. The ghost filter rejects partial-correlation sidelobes of a much stronger burst and
any duplicate decodes; this matters at the high SNR of a short lab link.

### Measured performance (simulation)

On the GRC PHY, running `grc/cdma_phy_sim.grc` in GNU Radio 3.10.12 (`tests.test_grc_flowgraph`, `grc-sim`):

| Test | Result |
|---|---|
| Lone frames, BPSK / QPSK / 16-QAM, unicast and broadcast, 300 Hz CFO | 7/7 delivered through the stock CRC-32 check, Es/N0 ≈ 46 dB |
| Three stations transmitting at the same instant on one carrier | 3/3 delivered (QPSK at 10–13 dB, BPSK at 16 dB: each link sees the other two as noise) |
| 200 KB photo through the full app (`grc-sim`, 1 MS/s, adaptive) | intact, 16-QAM, **40 kb/s goodput** (raw 16-QAM rate at 1 MS/s: 64.5 kb/s) |

The simulated graph runs at 1 MS/s because all three stations' Python blocks share one process. On hardware each
station has its own PC and runs the 4 MS/s rates above.

On the numpy PHY (`tests.test_modem`, `tests.test_profiles`, `tests.test_bigfile`):

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
sudo apt install gnuradio python3-numpy          # GNU Radio 3.10+: includes grcc and gr-iio (PlutoSDR blocks)
grcc --help | head -1                            # the app compiles grc/cdma_phy.grc with this at start
python3 -c "from gnuradio import iio; print('gr-iio ok')"
pip install cryptography                         # only needed for --key (encryption)
```

There are no other dependencies: the web server is pure Python standard library, and the UI loads nothing from the
internet. Copy this whole folder to each PC and keep `grc/`, `tools/` and `cdmachat/` together.

### 5.1 ANTSDR E200 setup

- **Pluto-compatible firmware (required by `grc/cdma_phy.grc`).** Set the boot DIP switch to QSPI. The default
  address is `ip:192.168.1.10`; set the PC's Ethernet port to `192.168.1.x`.
- **UHD firmware.** Set the boot DIP switch to SD and install MicroPhase's `antsdr_uhd` driver. The device args are
  `type=ant,addr=192.168.1.10`. Only the numpy PHY supports it (`run --phy numpy --radio uhd`); to use it with the
  flowgraph, replace the two PlutoSDR blocks in the .grc with UHD USRP Sink/Source blocks.
- **Several ANTSDRs on one switch** need different IPs. In the device shell run
  `fw_setenv ipaddr_eth 192.168.1.11` and reboot. With one PC per ANTSDR on a direct cable, all can stay at `.10`.
- **Antennas.** Use the TX1 and RX1 ports with separate 2.4 GHz-band antennas, a few cm apart, or more.
- **Bring-up order:**
  1. Check the PC can reach the device: `iio_info -u ip:192.168.1.10`.
  2. Check the GNU Radio install: `python3 -m cdmachat selftest --radio loopback`.
  3. Check the flowgraph compiles: `grcc -o build/grc grc/cdma_phy.grc` (the app does the same at start).

### 5.2 Mid-evaluation: simplex text link

```bash
# PC 2 (receiver)
python3 -m cdmachat rx --id 2 --uri ip:192.168.1.10
# PC 1 (transmitter): repeats every second, spread with node 2's code
python3 -m cdmachat tx --id 1 --to 2 --text "Hello from group 7" --uri ip:192.168.1.10
```

The receiver prints each message with its Eb/N0, carrier offset and preamble metric. These two small tools use the
numpy PHY (a minimal GNU Radio graph: PlutoSDR source/sink plus one Python block). Their bursts are compatible with
the flowgraph PHY, so `tx` can also send to a station running `run`.

### 5.3 Final system: one station per PC

```bash
python3 -m cdmachat run --id 1 --name Alice --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m cdmachat run --id 2 --name Bob   --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m cdmachat run --id 3 --name Carol --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m cdmachat run --id 4 --name Dave  --uri ip:192.168.1.10 --key "our-group-secret"
```

Each command compiles `grc/cdma_phy.grc` (you'll see `grcc` output and the PlutoSDR blocks connecting), starts the
flowgraph and the web UI. Then open **http://localhost:8080** on each PC. Stations appear within a second or two.
Node IDs are 1–4, because the flowgraph has one transmit lane per station code.

- Click a contact to chat; "Everyone" is the group.
- 📎 sends a photo or file. The mic button records a voice note of up to 15 s.
- ⚡ marks a message urgent. The # button starts tic-tac-toe.
- The bar-chart icon opens the radio dashboard.

Useful flags:

| Flag | Default | Notes |
|---|---|---|
| `--mod` | `auto` | Adaptive BPSK/QPSK/16-QAM, or force `bpsk`, `qpsk` or `16qam` |
| `--uri` | `ip:192.168.1.10` | The ANTSDR's libiio address (flowgraph parameter *ANTSDR URI*) |
| `--tx-atten` | 10 dB | 0 = full power (flowgraph parameter *TX attenuation*) |
| `--rx-gain` | 40 dB | Raise for distance; lower if the dashboard spectrum is clipping (*RX gain*) |
| `--freq` | 2.5 GHz | Every station must match (*Carrier*) |
| `--tx-buffer` | 131072 | ANTSDR TX buffer in samples (33 ms). Raise it if the dashboard shows TX underruns |
| `--ack-gap` | 0.15 s | Minimum pause after an ACK request, so the receiver can answer while the sender is silent. The link layer stretches it automatically to the measured round-trip time. |
| `--hd-defer` | 0.25 s | Holds new transmissions while a burst for us is arriving, to limit self-interference |
| `--host 0.0.0.0` | — | Lets other PCs view the UI. The microphone only works on `localhost`. |
| `--grc FILE` | `grc/cdma_phy.grc` | Run a different (e.g. edited copy of the) flowgraph |
| `--phy numpy` | `grc` | Fallback PHY without the .grc: the receiver and transmitter in numpy inside a minimal GNU Radio graph. Only this PHY uses `--radio uhd`, `--profile`, `--rate`, `--sps`, `--gold`, `--gain-mode` |

Sample rate, chip rate and code length are fixed by the flowgraph's variables (`samp_rate`, `sps`, `sf`). To change
them, edit the variables in GRC; every station must use the same file.

### 5.4 No hardware: two simulators

**The flowgraph PHY over a simulated channel** (`grc/cdma_phy_sim.grc`):

```bash
python3 -m cdmachat grc-sim                       # Alice, Bob, Carol on http://localhost:8081 … 8083
python3 -m cdmachat grc-sim --noise 0.05 --cfo 2000 --key secret
python3 -m cdmachat grc-sim --mod qpsk            # force a modulation
```

This compiles and runs `cdma_phy_sim.grc`: three copies of the station PHY whose transmit signals are added and
passed through a stock Channel Model (noise, carrier offset) to every receiver. It is the same chain as on
hardware, with the ANTSDR blocks replaced by the channel. It runs at 1 MS/s (rates are a quarter of hardware),
because all three stations' Python blocks share one process.

**The numpy channel simulator** (fast, up to 4 stations, per-link impairments):

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

The dashboard's mode line shows which PHY a station runs: "GNU Radio PHY · ANTSDR", "GNU Radio PHY · simulated
channel" or "simulation (numpy PHY)".

### 5.5 The GNU Radio Companion files (`grc/`)

| File | What it is |
|---|---|
| `cdma_phy.grc` | **The station's physical layer** on an ANTSDR E200 (Pluto/libiio firmware). Used by `run`. Parameters: Node ID, ANTSDR URI, carrier, RX gain, TX attenuation, TX buffer. |
| `cdma_phy_sim.grc` | Three copies of the same PHY (nodes 1–3) joined by a Channel Model with noise and carrier-offset variables. Used by `grc-sim`. |
| `cdma_phy_concept.grc` | DS-CDMA built only from stock blocks, with GUI plots. Two users with Gold codes #1 and #2 are summed with noise, then despread with the right and the wrong code. For the report and viva: it shows spreading, despreading and processing gain visually. Stand-alone (not used by the app). |

**How the app uses the .grc.** At start, `cdmachat/grc_phy.py`:

1. runs `grcc -o build/grc grc/cdma_phy.grc`, every time, so your last saved edit is what runs;
2. imports the generated `build/grc/cdma_phy.py` and creates its top block with the command-line values for the
   Parameter blocks (any parameter you delete simply keeps its default);
3. finds the Embedded Python blocks by node ID, attaches the link layer to **MAC → PHY** / **PHY → MAC**, and reads
   the dashboard data (constellation, spectrum, Es/N0, CFO, counters) from the CDMA Despreader and Burst Combiner;
4. starts the flowgraph.

**Editing it in GNU Radio Companion.** Open `grc/cdma_phy.grc` in `gnuradio-companion`. You can change variables
(gains, roll-off, `samp_rate`, `sps`), move blocks, add QT GUI sinks for your own measurements, or swap the PlutoSDR
blocks for UHD blocks; save, and the next `run` uses it. Keep in mind:

- The app needs the five Embedded Python blocks and the `code`/`mod` PDU routing between them. The constellation
  objects must keep their point order (it is the Gray mapping shared with the Framer's symbol table).
- The app sets the Parameter blocks, so it ignores **Run (F6)** values for those. Pressing F6 in GRC runs the PHY
  on its own: the MAC → PHY block then has no link layer, and PHY → MAC prints received frames to the console.
  That is a handy radio check, but a chat station is started with `python3 -m cdmachat run`.
- Keep `grc/` inside the project folder: the Embedded Python blocks import `cdmachat` from the folder above.

**Where the files come from.** `tools/make_grc.py` generates all three .grc files, with the Embedded Python block
sources in `tools/epy/`. Rerun it (`python3 tools/make_grc.py`) only if you want to regenerate them; it overwrites
edits made in GRC. `tools/render_grc.py` draws an approximate PNG of a .grc's layout without GNU Radio.

**Verification status.** All three files compile with GNU Radio 3.10.12's `grcc`, and the Embedded Python blocks'
ports resolve the way GRC's GUI sees them. `cdma_phy_sim.grc` runs end to end in GNU Radio (section 7), and the
chat app has run on it (200 KB photo, 16-QAM). The PlutoSDR blocks of `cdma_phy.grc` have not been run against
a physical ANTSDR yet, and the files have not been opened in the GRC GUI on a lab PC.

## 6. Demo plan for the final evaluation (≈ 10 min)

0. **The PHY.** Open `grc/cdma_phy.grc` in GNU Radio Companion and walk through it: CRC-32, the Framer, the
   five spreading lanes (Repeat × Gold code), RRC, ANTSDR sink; on receive the matched filter, Despreader and the
   three Constellation Decoder branches. `cdma_phy_concept.grc` (F6) shows processing gain live.
1. **Bring-up.** Start four stations with `run` and point out the `grcc` compile in the console. Show presence and
   the PHY line (2.5 GHz, 31-chip Gold, BPSK/QPSK/16-QAM, my code).
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
python3 -m tests.test_grc_flowgraph   # needs GNU Radio: compiles + runs cdma_phy_sim.grc, all modulations,
                                      # unicast/broadcast, 3 stations transmitting at once
python3 -m tests.test_grc_cores    # the flowgraph's Python cores with the stock blocks imitated in numpy,
                                   # plus numpy-PHY <-> GRC-PHY interoperability (no GNU Radio needed)
python3 -m tests.test_modem        # PHY: impairments, false alarms, multi-user, collisions, Eb/N0 sweep, CPU
python3 -m tests.test_profiles     # BPSK/QPSK/16-QAM per profile: SNR sweep, 2nd link, decode cost, real time
python3 -m tests.test_bigfile 10 fast 28 auto   # 10 MB transfer over the simulated air
python3 -m tests.test_link         # MAC over simulated air: concurrency, priority, lossy link
python3 -m tests.test_gr_backend   # numpy-PHY GNU Radio backend against a fake GR (API-shape test)
python3 -m cdmachat sim --nodes 3 & python3 -m tests.scenario_api   # full app scenario through the web API
```

`tests.scenario_api` also runs against `python3 -m cdmachat grc-sim` (same ports). These were run with GNU Radio
3.10.12. The ANTSDR blocks (`iio_pluto_sink/source`) have not been run on a physical E200 yet, so expect to tune
gains in the first lab session.

## 8. Troubleshooting

- **`grcc` not found / compile error at start.** Install GNU Radio 3.10+ (`grcc` comes with `gnuradio`). The
  console shows grcc's message; most often the .grc was edited into an invalid state. Open it in GRC to see the
  red blocks, or restore the shipped file.
- **`No module named 'gnuradio.iio'` / PlutoSDR block errors.** gr-iio is missing (Ubuntu: `gnuradio` 3.10 includes
  it; on older packaging install `gr-iio`). `iio_info -u ip:192.168.1.10` must list the device.
- **"the GNU Radio PHY has code lanes for node ids 1-4".** Use `--id` 1 to 4.
- **No stations appear.** Check `iio_info -u <uri>`, then check that every PC uses the same `--freq` and the same
  .grc variables. Open the dashboard spectrum: you should see a ~2.7 MHz-wide hump while another station transmits.
- **High Eb/N0 but many "CRC-32 drops" and retransmissions.** The radio link is fine; samples are being lost.
  Open the Counters panel. If **TX underruns** is climbing, the transmitter's buffer ran dry mid-burst, so frames
  went out with a hole in them. Raise `--tx-buffer 262144` (66 ms) and close heavy programs. If
  **RX overflows** is climbing, the PC can't keep up with receiving; see the next items.
- **"header CRC-8 fails" climbing, few frames.** The SNR is too low. Raise `--rx-gain`, lower `--tx-atten`, or move
  closer. If the spectrum is flat-topped or clipped, lower `--rx-gain`.
- **`rx_overflow_chunks` in the counters.** The PC can't keep up: the Despreader's input queue passed 3 s of
  samples and it dropped some. Close other programs. As a last resort lower `samp_rate` to 2e6 in the .grc on all
  stations (half the bit rate and half the CPU).
- **"Buffer too small for min_noutput_items" from a PDU to Tagged Stream block.** A burst is larger than the
  block's buffer. The shipped graph sets *Min Output Buffer* 16384 on the receive branches; keep it if you edit them,
  and raise it if you raise `max_payload`.
- **Retransmissions while two stations talk to each other at once.** Your own transmitter deafens your receiver.
  Separate the TX/RX antennas, lower TX power, or raise `--hd-defer`.
- **The microphone doesn't work.** Use `http://localhost`; browsers block the microphone on plain-HTTP remote
  addresses. You can attach an audio file instead.

## 9. Limitations and improvements (for the report)

- **No FEC.** The link relies on processing gain plus ARQ. A rate-½ convolutional or LDPC code would buy about
  5 dB, enough to run 16-QAM about 5 dB further out.
- **16-QAM needs a quiet channel.** 31-chip spreading suppresses another equal-power link by about 15 dB, while
  16-QAM needs about 20 dB. Adaptive modulation copes by stepping down to QPSK when the channel is busy.
- **Throughput ceiling.** About 235 kb/s goodput per link at 4 MS/s in the numpy simulator, limited by Python DSP
  CPU (not yet measured on hardware with the GRC PHY). A C++ OOT
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
- **Python DSP.** Synchronisation and despreading are numpy inside an Embedded Python block. Porting the
  correlator to a C++ OOT block would allow ~10 MS/s and remove the 1 MS/s limit of the three-station GRC simulation
  (its three PHYs share one Python interpreter lock).
- **Approximate CRC-drop counter.** On the GRC PHY the stock CRC32 block drops bad frames silently, so the dashboard
  counts them as "despread frames minus delivered frames".
- **Five code lanes.** `cdma_phy.grc` has transmit lanes for codes 0–4, so it supports node IDs 1–4 (the brief
  asks for up to four stations). More stations need more lanes (copy a lane, set its code variable and PDU Filter).

## 10. Suggested split for a group of four

| Member | Owns |
|---|---|
| A | PHY DSP: `codes.py`, `modem.py`, BER measurements, hardware gain tuning |
| B | GNU Radio and hardware: `grc/cdma_phy.grc`, `grc_phy.py`, ANTSDR firmware/IPs, `tx`/`rx` mid-eval demo |
| C | Link layer: `mac.py`, ARQ/priority tests, the simulator |
| D | Application and UI: `node.py`, `web/`, encryption, dashboard, demo script |

## Project layout

```
cdmachat/
  config.py      PHY parameters, address → code mapping
  codes.py       Gold codes, preamble patterns, RRC taps
  modem.py       burst modulator + streaming multi-code demodulator
  grc_phy.py     compiles + loads the .grc, cores of its Embedded Python blocks, GrcPhyRadio (MAC <-> flowgraph)
  radio_base.py  TxMixer (multi-code transmit), radio interface, dashboard data
  radio_gr.py    numpy-PHY GNU Radio graph (--phy numpy, tx/rx/selftest): ANTSDR via gr-iio or UHD, loopback
  radio_sim.py   multi-node channel simulator (numpy PHY)
  mac.py         link layer
  crypto.py      AES-256-GCM
  node.py        application layer
  web/           server.py + static/ (index.html, app.js, style.css)
  __main__.py    CLI: run | grc-sim | sim | tx | rx | selftest
grc/
  cdma_phy.grc          the station PHY (ANTSDR E200) used by `run`
  cdma_phy_sim.grc      three station PHYs + channel model, used by `grc-sim`
  cdma_phy_concept.grc  stock-block DS-CDMA demonstration with GUI plots
build/grc/       grcc output, regenerated at every start (not shipped)
tools/
  make_grc.py    generates the .grc files
  epy/           source of the five Embedded Python blocks
  render_grc.py  approximate PNG of a .grc layout (no GNU Radio needed)
tests/           GRC flowgraph, GRC cores + interop, modem, profiles, big file, link, GR backend, API scenario
```
