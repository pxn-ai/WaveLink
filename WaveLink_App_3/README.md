<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/wavelink-logo-dark.png">
    <img src="assets/wavelink-logo.png" alt="WaveLink" width="280">
  </picture>
</p>

# WaveLink Chat — multi-node DS-CDMA messaging over ANTSDR E200 + GNU Radio

By team **WaveLink** for the Communication Design Project (2026). A WhatsApp-style chat for 2–4 stations. Every station is one PC with
one ANTSDR E200. All stations share **2.5 GHz** using **direct-sequence CDMA** (31-chip Gold codes) with **BPSK,
QPSK or 16-QAM** payloads protected by **LDPC forward error correction** (the 5G NR code, rates ½ to ⅚, chosen per
link), so several links can be active at the same instant on the same carrier.

**The physical layer is a GNU Radio Companion flowgraph** (`grc/cdma_phy.grc`). When a station starts, the app
compiles it with `grcc`, runs it, and attaches its link layer and web UI to the flowgraph's two interface blocks.

```
 Browser (localhost:8080)                        one station = one PC + one ANTSDR E200
 ┌───────────────────────────────┐
 │ chats · photos · voice notes  │  web/        stdlib HTTP + Server-Sent Events
 │ files · emoji · themes · radio│
 │ dashboard (constellation,     │
 │ spectrum, counters)           │
 │ game center: UNO, Gold Code GP│
 └──────────────┬────────────────┘
 ┌──────────────┴────────────────┐
 │ Application    node.py        │  message envelope, AES-256-GCM, group fan-out, read receipts,
 │                games/         │  presence (beacons), profiles, clock offsets, history, media
 │                               │  store, game rooms
 ├───────────────────────────────┤
 │ Link layer     mac.py         │  addressing, fragmentation, pipelined selective-repeat ARQ,
 │                               │  adaptive modulation + coding, priority, duplicate filter
 ├────────── MAC -> PHY ─────────┤  ← Embedded Python blocks in the .grc (grc_phy.py)
 │ Physical layer                │
 │  grc/cdma_phy.grc             │  GNU Radio flowgraph: CRC-32, LDPC encoder, framing, Gold-code
 │  (compiled with grcc at start)│  spreading lanes, RRC, ANTSDR (Pluto) sink/source, despreading,
 │                               │  constellation soft decoders, LDPC decoder, CRC check
 └───────────────────────────────┘
```

## 1. Requirements checklist (from the project brief)

| Brief requirement | Where / how |
|---|---|
| **Mid-evaluation**: simplex text TX → RX | `python -m wavelink tx …` and `python -m wavelink rx …` (section 5.2) |
| Digital modulation | DS-CDMA with 31-chip Gold codes, RRC α = 0.35. BPSK preamble and Golay-coded header; payload **BPSK, QPSK or 16-QAM** with **LDPC rate ½, ⅔, ¾, ⅚ or uncoded**, chosen per link from the receiver's measured SNR (adaptive modulation and coding) |
| GNU Radio based | The PHY is `grc/cdma_phy.grc`, built from stock GNU Radio blocks plus seven Embedded Python blocks; the app compiles and runs it (section 5.5) |
| Two transceivers, real two-way link | ANTSDR E200 on each PC; full-duplex CDMA; ACKs flow on the reverse link |
| Packetization and reassembly | 1,010-byte fragments (`fast`), up to 65,535 per message (≈ 66 MB) — `mac.py` |
| Unique addressing | Node ID 1–4 = its Gold code (PHY: one code lane per station in the flowgraph) + `dst` byte checked in every frame (MAC) |
| CRC error detection | CRC-8 on the PHY header; CRC-32 on every frame (stock **Stream CRC32** blocks in the flowgraph) — bad frames are dropped |
| Error correction (extra) | **LDPC** (3GPP 5G NR base graph 1) on every payload, soft-decision min-sum decoding; extended **Golay (24,12)** on the header. Up to **4.8× goodput** on a 14 dB link, ~9 dB coding gain at rate ½ (section 2.2) |
| ACK-based reliable delivery | Pipelined selective-repeat ARQ: cumulative + selective (SACK) ACKs, fast loss detection, timeout + random back-off, duplicate detection |
| Payloads: text, images, audio | Text with an emoji palette; photos at a chosen quality (480, 640 or 1280 px WebP/JPEG, or the original); voice notes or audio files at a chosen quality (Opus 12 or 24 kb/s, or as recorded); plus any file, each cancellable from either end |
| Application | Chat with 1:1 chats, an "Everyone" group, delivery and read ticks, messages in the order they were sent, new-message highlights, profile pictures shared over the air, per-station themes, wallpapers and sounds (section 4.2), tic-tac-toe, and a **game center** with **UNO** and a real-time **street race with combat pickups**, each for 2–4 stations (section 4.1) |
| **Bonus**: encryption | `--key <passphrase>` → AES-256-GCM, scrypt KDF, addresses authenticated as AAD |
| **Bonus**: priority handling | 4 classes (control/ACK > urgent ⚡ > normal > bulk media); urgent text pre-empts an image between windows |
| **Bonus**: graphical dashboard | Radio panel: constellation, spectrum, Eb/N0, chip SNR, CFO, counters, queues, links |
| Other additions | Adaptive modulation and coding with SNR feedback in ACKs (hybrid ARQ type I); 10 MB+ file transfer; multi-code transmission to several stations at once; spread-ALOHA reception of colliding same-code bursts; presence beacons; channel simulator; GNU Radio Companion flowgraphs |

## 2. Physical layer

| Parameter | `grc/cdma_phy.grc` (default PHY) | numpy PHY `robust` profile |
|---|---|---|
| Carrier | 2.500 GHz (`--freq`) | same |
| Sample rate / RF bandwidth | 4 MS/s / 4 MHz | same |
| Chip rate | 2 Mchip/s (2 samples/chip) | 1 Mchip/s (4 samples/chip) |
| Spreading | 31-chip Gold codes, processing gain 14.9 dB | same |
| Symbol rate | 64.5 ksymbol/s | 32 ksymbol/s |
| Payload bit rate, uncoded BPSK / QPSK / 16-QAM | **64.5 / 129 / 258 kb/s** | 32 / 65 / 129 kb/s |
| Payload bit rate with LDPC | 32 kb/s (BPSK ½) … **215 kb/s (16-QAM ⅚)** | half |
| Forward error correction | LDPC, 5G NR base graph 1, rate ½ ⅔ ¾ ⅚ (or uncoded), one codeword per frame | same |
| Max frame payload | 1,024 bytes (MAC frame + CRC-32) | 512 bytes |
| Pulse shape | Root-raised cosine, α = 0.35, 8-chip span | same |

The flowgraph's rates are the numpy PHY's `fast` profile, so a GRC station and a `--phy numpy --profile fast`
station are compatible on air, coded or uncoded (`tests.test_grc_cores` checks both directions).

**Burst:** 48 known preamble symbols (BPSK, code-specific) · header: 24 bits (11-bit length, 2-bit modulation,
3-bit code rate, CRC-8) sent as **two extended Golay (24,12) codewords = 48 coherent BPSK symbols** · payload: the
LDPC codeword (or the plain bits of an uncoded frame), Gray-mapped to BPSK, QPSK or 16-QAM, padded to a multiple of
16 symbols. The receiver reads modulation and code rate from each header, so both can change on every frame.
(Uncoded BPSK is differentially coded on the numpy PHY and coherent on the GRC PHY; coded BPSK is always coherent.)

### 2.1 The flowgraph (`grc/cdma_phy.grc`)

When a station starts, `python3 -m wavelink run` compiles `grc/cdma_phy.grc` with `grcc` into `build/grc/`
(every time, so edits saved in GRC take effect on the next start), imports the generated `top_block`, sets its
parameters (node ID, ANTSDR URI, carrier, gains) and runs it. The link layer talks to the flowgraph only through the
**MAC → PHY** and **PHY → MAC** blocks. Everything between them is the flowgraph.

```
TX  MAC -> PHY ─► PDU to Tagged Stream ─► Stream CRC32 (append) ─► Tagged Stream to PDU ─► LDPC Encoder
    ─► CDMA PHY Framer
    ─► 5 code lanes (code 0 = broadcast, 1–4 = nodes), each:
         PDU Filter (code = k) ─► PDU to Tagged Stream ─► Chunks to Symbols (BPSK/QPSK/16-QAM table)
         ─► Repeat ×31 ─► Multiply ◄─ Vector Source (Gold code k) ─► Tagged Stream Multiply Length
    ─► CDMA Burst Combiner ─► Interpolating FIR (RRC, ×2) ─► Multiply Const ─► PlutoSDR Sink (ANTSDR TX1)

RX  PlutoSDR Source (ANTSDR RX1) ─► FFT Filter (RRC matched filter) ─► CDMA Despreader
    ─► 3 modulation branches (BPSK, QPSK, 16-QAM), each:
         PDU Filter (mod) ─► PDU to Tagged Stream ─► Constellation Soft Decoder (soft bits)
         ─► Tagged Stream Multiply Length (× bits/symbol) ─► Tagged Stream to PDU
    ─► LDPC Decoder ─► PDU to Tagged Stream ─► Stream CRC32 (check, bad frames dropped)
    ─► Tagged Stream to PDU ─► PHY -> MAC
```

| Block | Kind | Job |
|---|---|---|
| MAC → PHY, PHY → MAC | Embedded Python | Hand frames between the app and the flowgraph as PDUs (metadata: `code`, `mod` = modulation and coding, e.g. `16qam-3/4`; on RX also SNR, CFO, LDPC iterations) |
| LDPC Encoder | Embedded Python | 5G NR LDPC encoding of frame + CRC-32, rate matching to the requested rate, 5G bit interleaver (section 2.2) |
| CDMA PHY Framer | Embedded Python | Builds the burst as constellation **indices**: preamble, Golay-coded header, coded payload bits → symbol numbers in `sym_table` (0–1 BPSK, 2–5 QPSK, 6–21 16-QAM) |
| CDMA Burst Combiner | Embedded Python | Adds the 5 code lanes chip by chip (several stations served at once) and sends zeros between bursts, so the ANTSDR always has samples |
| CDMA Despreader | Embedded Python | Synchronisation: preamble correlation on this node's code and the broadcast code, CFO estimate, fractional-delay despreading with DLL timing, Golay header decode, carrier tracking. Outputs the corrected payload symbols |
| LDPC Decoder | Embedded Python | Undoes interleaving and rate matching, layered normalised min-sum decoding with early stop; uncoded frames get hard decisions |
| Everything else | Stock GNU Radio | CRC-32, spreading (Repeat × Gold code), Chunks to Symbols, pulse shaping, ANTSDR sink/source, matched filter, **Constellation Soft Decoder** with `variable_constellation` objects (soft bits for the LDPC decoder), length-tag handling |

Modulation and coding switching: the link layer chooses modulation and code rate for each frame (adaptive, or
restricted with `--mod` / `--fec`) and puts it in the PDU's `mod` field (`qpsk-1/2`, `16qam-3/4`, `16qam`, …). The
LDPC Encoder encodes at that rate, the Framer maps the bits to the right part of the symbol table and writes
modulation and rate into the header. On receive, the Despreader reads the header and tags the PDU with modulation,
rate and length; a PDU Filter routes it to the matching Constellation Soft Decoder, and the LDPC Decoder uses the
rate and length to decode.

The Embedded Python blocks keep their DSP in `wavelink/grc_phy.py` and `wavelink/modem.py`, so the code inside the
.grc stays short. Their source lives in `tools/epy/`; `tools/make_grc.py` writes it into the .grc files.

### 2.2 Forward error correction (LDPC)

**The code.** The data channel code of 5G NR (3GPP TS 38.212, base graph 1): a quasi-cyclic LDPC code whose 46 × 68
base matrix is lifted by a factor Z (51 allowed sizes from 2 to 384) into a sparse parity-check matrix H. The table
(`wavelink/nr_bg1.py`) was taken from NVIDIA Sionna and checked entry by entry against AFF3CT's lifted matrices for
all 51 lifting sizes (`tools/gen_nr_bg1.py`); `tests.test_ldpc` checks that every encoded word satisfies all 46 Z
parity checks.

**Per frame** (`wavelink/ldpc.py`):

1. The frame (MAC frame + CRC-32, ≤ 1,028 bytes = 8,224 bits) is **one codeword**: Z is the smallest lifting size
   with 22 Z ≥ K, and the free positions are filled with known zeros ("filler bits", never sent). A 1 KB frame uses
   Z = 384; a 30-byte ACK uses Z = 11.
2. **Systematic encoding** using the structure of the 5G core: the four core parity blocks follow from the
   column sums of the information part; every extension parity block is one row's sum.
3. **Rate matching** (5G redundancy version 0): the first 2 Z information bits are punctured (as in 5G), fillers are
   skipped, and the first E bits are sent, E = K / R rounded up to whole symbols. Every rate is a prefix of the same
   mother codeword, so one matrix serves rates ½, ⅔, ¾ and ⅚ — the decoder just uses fewer rows at higher rates.
4. The **5G bit interleaver** (Qm rows) spreads each codeword across the bit positions of the QPSK/16-QAM symbols.

**Decoder:** soft bits from a max-log demapper (numpy PHY) or the stock Constellation Soft Decoder (GRC PHY), then
**layered normalised min-sum** (α = 0.75): row layers that share no variables are merged, every layer is processed as
one vectorised numpy operation over its Z check nodes, and decoding stops as soon as all parity checks pass (most
frames at good SNR need 1–3 iterations; at most 20). Min-sum is scale-invariant, so the decoder needs no noise
estimate. A 1 KB frame decodes in 3–7 ms at rate ⅚ and 15–25 ms at rate ½ (up to ~50 ms right at the threshold).

**Header:** the 24 header bits are two extended Golay (24,12) codewords (d_min = 8), decoded by soft maximum
likelihood (correlation with all 4,096 codewords). It works down to ~1 dB symbol SNR, so the header never limits a
coded frame. The header is coherent (phase from the preamble fit); after it is decoded its 48 symbols become known
and the carrier frequency is re-estimated over all 96 known symbols, which keeps 16-QAM ½ locked at 9 dB.

**Adaptive modulation and coding.** Each ACK carries the SNR the receiver measured on the frame that requested it.
The sender picks the fastest entry of this table whose SNR requirement is met (`mac.MCS_TABLE`; despread symbol SNR
for ~100 % delivery of 1 KB frames plus ~0.5–1 dB margin, measured with the full receiver):

| Rung | Info bits/symbol | `fast` profile | Needs Es/N0 |
|---|---|---|---|
| QPSK ½ (also ACKs, beacons, broadcasts) | 1.0 | 65 kb/s | — (floor; works from ~4–5 dB) |
| QPSK ¾ | 1.5 | 97 kb/s | 6.0 dB |
| QPSK ⅚ | 1.67 | 108 kb/s | 7.0 dB |
| 16-QAM ½ | 2.0 | 129 kb/s | 9.5 dB |
| 16-QAM ⅔ | 2.67 | 172 kb/s | 11.0 dB |
| 16-QAM ¾ | 3.0 | 194 kb/s | 12.5 dB |
| 16-QAM ⅚ | 3.33 | 215 kb/s | 14.0 dB |
| 16-QAM uncoded | 4.0 | 258 kb/s | 22.5 dB |

It drops one rung for 10 s whenever windows keep losing more than 20 % of their frames, and messages of one or two
fragments go two rungs lower (a lost frame costs a whole round trip). Interference counts: with another
equal-power link on the channel the measured SNR falls to ~12–15 dB, and the link moves from uncoded 16-QAM to
16-QAM ¾ or ⅔ instead of dropping to QPSK. `--mod bpsk|qpsk|16qam` restricts the modulation, `--fec off|1/2|2/3|3/4|5/6`
the coding (`--fec off` gives the old uncoded BPSK/QPSK/16-QAM behaviour). ACKs use QPSK ½: the airtime of uncoded
BPSK with ~9 dB more margin.

Low-SNR limit: coded QPSK ½ could decode at ~1.5 dB, but burst detection needs ~4 dB (detector threshold 0.6 plus
a second, coherent preamble check that rejects noise and other codes' data). A longer preamble would extend this.

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
4. CFO removal, then a phase/frequency fit over the known symbols (preamble; after the header is decoded,
   preamble + header). A second, coherent check of every detection against the known preamble (4 segments)
   rejects noise and other codes' data before any decoding is attempted.
5. **Fractional-delay despreading.** Each symbol is despread at a few whole-sample offsets, and an 8-tap
   windowed-sinc filter interpolates between them to 1/64-sample timing resolution. That accuracy is what 16-QAM
   needs; without it, inter-chip interference caps the SNR near 12 dB. An early–late **DLL** tracks the
   sample-clock offset between the two radios, up to 30 ppm.
6. **Carrier tracking.** Uncoded BPSK uses a block-wise squaring estimate plus differential decoding. QPSK, 16-QAM
   and coded BPSK use a decision-directed block tracker (a 2nd-order loop) anchored on the known preamble and header,
   so there is no 90° ambiguity to slip into.
7. Golay header decoding + CRC-8, then soft demapping, LDPC decoding and the CRC-32 check. On the GRC PHY these are
   the stock Constellation Soft Decoder, the LDPC Decoder block and the stock Stream CRC32 block.

Spurious detections are rejected four ways: a 48-symbol preamble with a 0.6 threshold, the coherent second check,
code-specific preamble patterns, and a "ghost" filter. The ghost filter rejects partial-correlation sidelobes of a
much stronger burst and any duplicate decodes; this matters at the high SNR of a short lab link. 3 s of pure noise
on two codes gives 0 detections.

### Measured performance (simulation)

On the GRC PHY, running `grc/cdma_phy_sim.grc` in GNU Radio 3.10.12 (`tests.test_grc_flowgraph`, `grc-sim`):

| Test | Result |
|---|---|
| Lone frames: uncoded BPSK/16-QAM and LDPC QPSK ½ ¾, 16-QAM ⅔ ¾ ⅚, BPSK ½; unicast and broadcast | 10/10 delivered through the stock soft decoders, LDPC decoder and CRC-32 check |
| Three stations transmitting at the same instant on one carrier | 3/3 delivered, **including 16-QAM ½ at 12 dB** (each link sees the other two as noise; uncoded 16-QAM needs ~20 dB) |
| Noisy channel, Es/N0 ≈ 6 dB | QPSK ½ and ¾ delivered; uncoded QPSK and 16-QAM ½ lost, as expected |
| 100 KB file, Es/N0 ≈ 12 dB, MAC + ARQ (`tests.test_grc_goodput`) | **37.8 kb/s with adaptive LDPC (16-QAM ⅔) vs 15.6 kb/s uncoded (BPSK): 2.4×**, 0 retransmissions |
| 100 KB file, clean channel (≈ 46 dB) | 48 kb/s adaptive (mostly uncoded 16-QAM, ¾ after a loss) vs 34 kb/s with `--fec off` |
| Full app scenario (`grc-sim` + `tests.scenario_api`) | passed: text, group, image, game, read receipts |

The simulated graph runs at 1 MS/s because all three stations' Python blocks share one process. On hardware each
station has its own PC and runs the 4 MS/s rates above.

On the numpy PHY (`tests.test_ldpc`, `tests.test_modem`, `tests.test_profiles`, `tests.test_bigfile`):

| Test | Result |
|---|---|
| LDPC alone, BPSK on AWGN, 1 KB frames | rate ½: 0/20 frame errors at **Eb/N0 1.5 dB**; ¾ at 2.5 dB; ⅚ at 3.5 dB (uncoded needs ~10.5 dB) |
| Full receiver, 1 KB frames, symbol SNR for ~100 % delivery | QPSK ½ ~5 dB (detection-limited), QPSK ¾ 5.5, QPSK ⅚ 6, 16-QAM ½ 9, ⅔ 10, ¾ 11, ⅚ 12.5, uncoded 16-QAM 20–22 dB |
| Second equal-power link on another code (Es/N0 20 dB) | uncoded 16-QAM 2/4 frames; **16-QAM ¾ and ⅚ 4/4** |
| QPSK ½, 200-byte frames, CFO ±12 kHz, 20 ppm clock offset | 10/12 at Es/N0 4 dB, 11/12 at 6 dB (uncoded BPSK, same bit rate: 6/12 at 8 dB, 12/12 at 10 dB) |
| Implementation SNR ceiling (receiver accuracy) | about 44 dB for every modulation |
| 3 s of pure noise on two codes | 0 detections, 0 frames |
| Two simultaneous links, interferer +10 dB (BPSK) | both decoded (near-far tolerance) |
| Two senders → same receiver, overlapping | 20/20 bursts decoded |
| Receive + decode cost per 1 KB frame (incl. LDPC) | 27–97 ms for 33–128 ms on air; one receiver keeps up with continuous maximum-rate traffic of every rung (1.1–1.6× real time on a 2-core VM) |
| **2 MB file, `fast`, Es/N0 28 dB, adaptive** | **67.7 s, 236 kb/s goodput, 0 retransmissions** (uncoded 16-QAM) |
| **1 MB file, Es/N0 14 dB** | **176 kb/s with LDPC (16-QAM ¾) vs 37 kb/s uncoded (BPSK): 4.8×**, 0 retransmissions either way |
| 10 MB file, 28 dB (before FEC was added; same rung) | 340 s (5 min 40 s), 235 kb/s goodput, intact, 0 retransmissions |

## 3. Link layer (MAC)

```
DATA   | type | flags | dst | src | msg_id(2) | frag_idx(2) | frag_count(2) | payload ≤1010             | CRC-32 |
ACK    | type | flags | dst | src | msg_id(2) | frag_count(2) | base(2) | trigger(2) | snr(1) | SACK   | CRC-32 |
BEACON | type | flags | 255 | src | 0         | {"n": name, "e": key-id}                              | CRC-32 |
DGRAM  | type | flags | dst | src | 0         | payload (game clock sync, race car states)            | CRC-32 |
flags: bits 0–1 priority, bit 2 ACK-request
```

- **Addressing.** Frames whose `dst` is neither me nor 255 are dropped and never ACKed. Frames whose `src` is me
  are dropped too, since a station hears its own transmitter.
- **Reliability: pipelined selective-repeat ARQ.**
  - The sender streams fragments and sets ACK-request on every *W*-th frame, with *W* chosen so a window is about
    0.5 s on air (≈ 16 frames at uncoded 16-QAM, ≈ 12 at 16-QAM ¾).
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
  - Together with the LDPC code this is **hybrid ARQ type I**: the code corrects most errors, the CRC-32 catches
    what it cannot, and ARQ resends those frames (adaptive modulation and coding, section 2.2, keeps that rare).
- **Priority.** Messages to the same station are served in priority order, and at most about one window of airtime
  is queued per code. An urgent text therefore gets in front of a 10 MB transfer within about 0.5 s. ACKs always go
  first. On the numpy PHY, high-priority frames (urgent text, game messages) also go ahead of frames already waiting
  on the radio, in order among themselves; the GNU Radio flowgraph's code lanes are first-in first-out.
- **Presence.** A beacon every 5 s (±20 %) on the broadcast code. A contact goes offline after 17.5 s in which
  nothing was heard from it (no beacon and no other frame, so busy game traffic can't make a station look gone).
- **Datagrams.** A `DGRAM` frame is sent once, never ACKed or repeated, at the most robust modulation and coding
  (QPSK ½), handed straight to the PHY instead of the ARQ queues. The game center uses them where a late copy is
  useless: clock-sync pings and race car states (section 4.1). Race states go in time slots; the simulated radio can
  start a burst on an exact sample (`send(at=…)`). A queued message that has not started can also be *withdrawn*;
  the UNO host uses that to replace a player's view that a newer one has made stale.

## 4. Application layer

Each message envelope is `crypto byte | meta length | meta JSON | body`. The meta JSON holds the kind, uid,
timestamp, group, filename and mime type.

- **Kinds:** `text`, `image`, `audio`, `file`, `game` (tic-tac-toe), `rcpt` (read receipt) and `gc` (game center,
  section 4.1; not kept in the history). Text, and files that compress (documents, CSV, logs), are zlib-compressed
  before sending. Already-compressed data is sent as it is.
- **Large files:** up to 64 MB per message. The UI shows an airtime estimate at the link's current modulation, a
  progress bar with retries on the sender's bubble, and "receiving n/N fragments" on the receiver, each with a ✕
  that cancels the transfer on both ends (section 4.2).
- **Order:** a chat is sorted by when each message was *sent*, converted to this station's clock, so a message
  that arrives late slides into its place (section 4.2).
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
  | ⊘ | cancelled (by you, or "cancelled by …" when the recipient stopped it) |

- **Encryption:** AES-256-GCM, with the key = scrypt(passphrase). Each message uses a random 96-bit nonce, and
  `(src, dst)` is the associated data. The UI shows a short key-ID, and warns when a peer's beacon advertises a
  different key.
- **History:** stored in `data/node<N>/history.jsonl`, with media files in `data/node<N>/media/`.

### 4.1 Game center (`wavelink/games/`)

The gamepad button at the top of the chat list opens the game center. It has two games, each for 2, 3 or 4
stations, played with the keyboard:

| | UNO | Gold Code GP |
|---|---|---|
| Game | The card game, standard rules (no house rules) | A top-down street race through a rain-soaked night city, 2, 3 or 5 laps, with combat pickups |
| Who decides | The host's station: it shuffles, deals, keeps every hand and checks every move | Each station drives its own car. A hit is decided by the station of the car that was hit |
| On the air | Reliable game messages (ARQ), one view per player per move | One unacknowledged datagram per car per time slot, on the broadcast code |
| Look | Plum velvet table with brass trim, original card faces set in Lilita One | Sodium street lamps and neon on wet asphalt in the rain, Chakra Petch HUD |

**Rooms.** Pick players on a game's poster and press *Invite*. The others get an invitation (a toast, a dot on the
gamepad icon and a card in the game center) and press *Join*; the host starts once at least one has joined
(stations that haven't answered stay out). Invitations, answers, the lobby, the start and the end go as `gc`
messages: reliable, sent at high priority so they overtake chat and file traffic, and not stored in the chat
history. A player who leaves or goes off the air is announced and the game goes on without them; if the host goes,
the room closes. **Chat** in the game center's top bar (with the number of unread messages) opens a panel at the
side: pick a chat, read, and send, while the game goes on behind it. Keys typed in the panel never reach the game
(so typing "d" doesn't draw a UNO card); in the race your car rolls on without steering while you type, and the
other stations keep hearing it. <kbd>Esc</kbd> closes the panel. Closing the game center (<kbd>Esc</kbd>) keeps a running game going behind the chat; the gamepad
icon brings it back.

**UNO.**
- Rules: 7 cards each; match the colour or the number/symbol, or play a Wild. Skip, Reverse (a Skip with two
  players), Draw Two, Wild, Wild Draw Four (only when you hold no card of the current colour; the host checks it, so
  there is no challenge). Instead of playing you can draw one card, then play it or keep it. Call UNO when you play
  your second-to-last card; until the next player acts, anyone can catch an uncalled UNO for a 2-card penalty. The
  winner scores the cards left in the other hands (numbers at face value, action cards 20, wilds 50); the host deals
  the next round.
- Hidden hands: each player receives a *view* with only their own hand and the others' card counts. A move is an
  intent (`play card 37 as blue`, `draw`) that the host checks; an illegal move comes back as a message.
- 30 s per turn, then the host plays the timeout for that player (draw and pass, keep a drawn card, or pick a
  colour).
- Game messages are high priority. On the numpy PHY their frames also go ahead of file-transfer frames already
  waiting on the radio, so a move still shows on every screen in about 0.3 s while a 400 KB file goes to one of the
  players (table below). The GNU Radio flowgraph's code lanes are first-in first-out: there a view waits in the MAC
  queue while the lane holds a window of file frames (up to 0.6 s). If the player's next view is ready by then, the
  host withdraws the old one and sends one view that carries the events of both, so only the newest state goes on
  the air and no event is lost.
- Keys: <kbd>←</kbd>/<kbd>→</kbd> choose a card, <kbd>Enter</kbd> play, <kbd>D</kbd> draw, <kbd>P</kbd> keep the
  card you drew, <kbd>U</kbd> call UNO, <kbd>C</kbd> catch, <kbd>1</kbd>–<kbd>4</kbd> or <kbd>R</kbd><kbd>Y</kbd>
  <kbd>G</kbd><kbd>B</kbd> pick a colour, <kbd>N</kbd> deal the next round (host).

**Gold Code GP.**
- The circuit is generated from the room's seed (a closed spline through 9–12 random points, rejected and redrawn
  if a corner is too tight or two parts of the track come too close), so every station builds the same track and
  city without sending them. A new track every race.
- Each station runs its own car's physics at 120 steps per second in the browser and posts the car's state to its
  station 20 times a second.
- **Time slots.** The station puts the newest state on the air as a 28-byte datagram (+13 bytes per event) in its
  own slot: seat *k* of *N* transmits at *S₀ + k·P/N + m·P* on the host's radio clock, with
  *P* = max(1/15 s, *N* × airtime × 1.7). The cars' bursts never overlap on the broadcast code, and no station is
  transmitting (deaf) while another car's update is on the air. At the default chip rate a state with one event is
  a 52-byte frame, 7.9 ms on air, so every car gets 15 updates a second with 2, 3 or 4 cars. With `--key` the frame
  is 80 bytes (11.4 ms): still 15 a second for 2–3 cars, 12.9 for four.
- **Clocks.** The countdown, the timestamps and finish times use the host's clock. Each other player measures its
  offset with datagram pings, keeping the sample with the shortest round trip; the host can only start the race once
  every player is synchronised. The same pings synchronise the radio clock the slots use. On the ANTSDRs that is the
  same clock; in the simulator it is the simulated air's sample counter, which falls behind real time when the PC
  can't keep up, so there a burst is handed to the simulated radio up to 20 ms early with its exact start sample.
- **Dead reckoning.** The other cars are drawn where their last state says they are *now*. The prediction runs
  in track coordinates (distance along the centreline, offset across it): a car that was following the track keeps
  following it at its speed along the track for up to 1.5 s, its sideways drift fades out, and its offset is
  limited to the rails, so a remote car can never be drawn through a wall even after a second of lost packets. A
  spinning, stunned, slow or sideways car gets a short straight-line guess instead. When a new state disagrees
  with the prediction, the difference is blended out along the track over 0.16 s. The sender also posts a smoothed
  yaw rate. The HUD shows, per car, the updates heard per second and the age of the last one.
- **Camera.** North up (the car turns, the camera looks ahead of it) or chase (<kbd>V</kbd>, or the button in the
  top bar, or Settings): the car stays at two thirds of the screen height pointing up and the city turns around it.
  The camera follows the car's heading with a short lag (longer for a big turn, and it holds still while the car
  spins after a hit). Names stay upright; the minimap stays north up and shows your car as an arrow. The static
  city is drawn once into 512 px tiles with a 2 px overlap, so turned tiles leave no seams. Without a GPU, a turned
  picture costs the browser more, so the canvas drops to 90, 80 or 70 % of the screen resolution while the frame
  rate is under 45 and goes back up when there is time to spare.
- **Sounds:** countdown beeps and the green light, an engine that follows speed and throttle, pickups, missile,
  mine, explosions, EMP, shield, boost and boost pads, scraping the rails, lap and finish (section 4.2).
- **Pickups** come from holo caches on the track, weighted by position (the last car gets more missiles, the
  leader more mines and shields): seeker missile (follows the track and locks on to the car ahead), plasma mine
  (dropped behind you), EMP (stuns every car within 280 px for 1.4 s), shield (blocks the next hit), boost. Boost
  pads on the track give a short boost too.
- **Events** (missile fired, mine dropped, EMP, hit, finish) ride in the state packets and are repeated in the
  next 4 packets, so one lost packet loses nothing; receivers drop the copies.
- **Hits are decided by the victim's station**, which knows exactly where its car was. It simulates the incoming
  missile against its own position, checks mines when it drives over them, and checks an EMP against where it was
  at the moment the EMP went off (it keeps the last 2 s of its positions). It then broadcasts a *hit* event, and
  every station shows the explosion and removes the projectile.
- Each finish time goes to every station both as an event and as a reliable message. The host publishes the
  results when everyone has finished, 25 s after the first car finishes, or when fewer than two cars are left. The
  host can start a rematch on a new track from the results.
- Keys: arrows or <kbd>W</kbd><kbd>A</kbd><kbd>S</kbd><kbd>D</kbd> to drive, <kbd>Space</kbd> to use the pickup,
  <kbd>V</kbd> to switch the camera.

**Measured** (`tests/test_games.py`, numpy simulator, Es/N0 25 dB):

| | 2 stations | 3 stations | 4 stations |
|---|---|---|---|
| UNO: a move until every screen shows it | median 0.08 s, 90 % 0.11 s | median 0.16 s, 90 % 0.21 s | median 0.29 s, 90 % 0.48 s |
| UNO while a 400 KB file goes to one player | | median 0.27 s, 90 % 0.38 s | |
| Race: updates heard per car per second | 8.7–12.8 | 4.3–5.4 | 3.0–3.1 (2.5 with `--key`) |
| Race: state age when heard, median | 0.06 s | 0.08–0.11 s | 0.11–0.16 s |
| Race: packets lost, events missed, duplicates shown | ≤ 4 %, none, none | ≤ 1 %, none, none | ≤ 1 %, none, none |
| Simulator: CPU seconds per second of air | 0.8 | 1.9 | 4.8 |

One PC computes every receiver in the simulator, and above one CPU second per second of air the simulated air runs
slower than real time. That, not the slots, limits the race update rate there; on hardware the slots give 15 per
second. A UNO round (and the deal of the next one), a full race and a rematch with the chase camera were also
played through the browser UI on the numpy simulator with 2, 3 and 4 stations (`tests/ui_games.py`, keyboard input
in headless Chromium); no remote car was ever drawn beyond the rails. A race page drew 60 frames per second (the
display rate) at 1440 × 900 in a headless browser without a GPU, next to a simulator: north up at full resolution,
chase at 54–60 after dropping to 90 % resolution (36–44 at full resolution). With every station's page and the
simulator sharing one 2-core VM, each page drew 30–40 frames per second with two stations, 15–21 with three (8
with the chase camera) and 7–13 with four.

The game traffic uses only the radio interface both PHYs share (send, airtime, transmit backlog, clock). It has not
yet been run on the GNU Radio PHY (`grc-sim`) or on the ANTSDRs.

Both typefaces are bundled as subsets in WOFF format under the SIL Open Font License (`web/static/FONTS-OFL.txt`), so
the game center works offline like the rest of the UI. The Lilita One subset is renamed "Cardface", as its Reserved
Font Name requires.

### 4.2 Profiles, looks and the finer points of the chat

Everything below is in the browser and the station; nothing changes in the PHY or the MAC except two small
message kinds (`prof`, `ctl`) and two fields in the beacon. **Settings** open from the gear button (or by clicking
your own name or picture):

| Setting | Choices | Kept |
|---|---|---|
| Name and picture | your initial or an emoji on a colour of your choice, or a photo (cropped square, 160 px WebP, about 5 KB) | `data/node<N>/profile.json`; goes to the other stations over the air |
| Theme | eight palettes (WaveLink, the default, from the logo's navy, blue and cyan; Lagoon, Saffron, Orchid, Cobalt, Coral, Graphite, Neon) or a palette made from any colour; light, dark or the system's choice; breathing lights on or off | profile.json, plus a copy in the browser so the first paint already has the right colours |
| Chat wallpaper | seven patterns drawn in CSS from the palette (plain, Gold-code chips, QAM constellation, waves, rain, dusk, velvet) or your own photo with a dimmer | this station only (`data/node<N>/wallpaper.*`) |
| Sounds | sound effects on or off, volume; background music (four built-in loops or your own MP3) with its own volume | profile.json; an uploaded MP3 stays on this station (`data/node<N>/music.*`) |
| Photos and audio | choose the quality each time, or always send at a default | profile.json |
| Gold Code GP | north-up or chase camera | profile.json |
| Buttons | Game center, Radio and Settings next to your name, or in a bar with their names at the bottom of the chat list | profile.json |

Each station keeps its own settings, so two people at two stations can have different looks.

**Profile pictures over the air.** The beacon (every 5 s) carries the name and a short picture tag: `e:<hue>:<emoji>`,
`i:<hue>`, or `p:<hash>` for a photo (the first 10 hex digits of its SHA-1), about 14 bytes. A station that hears a
photo hash it doesn't have asks the owner for the photo once with a `prof` message (reliable, normal priority; at
most one request per 45 s), the owner answers with the picture, and the receiver keeps it only if the hash matches.
A photo therefore crosses the air once per pair of stations, and again only when it changes. With `--key` the
photo is encrypted like any message; the tag and the name in the beacon are not.

**The WaveLink look: light that follows the signal.** The team logo (cut out of its background by
`tools/make_logo.py`) heads the chat list, fills the empty screen and the About card in Settings, and is the
browser tab's icon; the wordmark is set in Montserrat in the logo's colours. Every palette has two accents, and
everything active (send button, badges, switches, chosen options, progress bars, the radio dashboard's numbers and
spectrum) is drawn in their gradient, while surfaces are lightly shaded from the top. Light then does two things:

| | What | Where |
|---|---|---|
| Breathing (slow, idle) | presence and readiness | the logo's halo, online dots and the ring of light round an online contact's picture, unread badges, the send button once there is text, the line beside the open chat, your picture while your radio hears frames |
| Pulsing (an event) | something went on the air | the logo sends out a radio ring when frames are heard or a message comes or goes (at most every 1.2 s), and so does the light in the Radio panel; a light passes over each new bubble; your bubble glows while it is on its way and stops when it is delivered; new messages breathe until seen; the header's signal line flows while a file goes to or from that chat; the new-message arrow sends out rings |

The empty screen shows the logo sending out waves over a slow aurora, with who is on the air. The animations use
the compositor where they can (transform and opacity) and pause behind the game center; with every effect running
an idle page keeps the main thread about 5 % busy in a headless browser without a GPU (under 1 % with them off).
The switch in Settings, or the system's "reduce motion", stops all motion; the shading stays.

**Emoji palette** (the smiley in the composer): 1,167 emoji in nine groups, including a "Radio" group (📡 📶 🛰 …).
Search by the Unicode names plus a few aliases ("lol", "fire", "antenna"), recently used first (kept in the
profile), arrow keys and Enter work, Esc closes. A message of one to three emoji and nothing else shows large.

**Quality before sending.** Picking a photo or an audio file, or finishing a voice note, opens a sheet that has
already made every version, with each one's size and time on air at the link's current modulation and a preview of
the chosen one:

| | Photo | Voice note or audio file |
|---|---|---|
| Choices | Saver (480 px), Standard (640 px), HD (1280 px), Original | Low (Opus 12 kb/s), Standard (Opus 24 kb/s), High (as recorded, 48 kb/s) or Original for a file |
| Made by | drawing on a canvas and encoding WebP (JPEG where the browser can't write WebP) | decoding to 48 kHz mono, re-encoding with the browser's WebCodecs Opus encoder (60 ms frames at 12 kb/s) and packing the packets into WebM with a small muxer in `media.js` (EBML header, one Opus track with its OpusHead, clusters of SimpleBlocks, the duration in the header so players show the length); browsers without WebCodecs replay the clip into a MediaRecorder at the lower rate |

Turning "Choose the quality each time" off sends at the default from Settings, or at the next smaller size when
that one is too big for the link. Voice notes can be up to 30 s long, with a live level meter while recording.

**Taking a photo.** The camera button next to the microphone opens a live preview (mirrored for the front camera,
with a switch when there are two cameras). The shutter (or <kbd>Space</kbd>) takes the picture at the camera's full
resolution; Retake goes back to the live picture, Use photo passes it to the quality sheet above (or sends it at
the default). Like the microphone, the camera works when the page is opened as `http://localhost` (or HTTPS).

**Cancel.** A file being sent has ✕ next to its progress bar. The station takes the message out of the transmit
queue and, if fragments had already gone out, sends a `ctl` *cancel* message, so the receiver drops the partial
message and ignores the frames still in flight. The receiver has ✕ next to "receiving n/N fragments": it drops the
message and sends a `ctl` *stop*; the sender stops and the bubble says "cancelled by Bob". The tick becomes ⊘.

**Messages in the order they were sent.** Every message carries its send time on the sender's clock. Each station
estimates every other station's clock offset from the send times in its beacons and short messages: the smallest
"arrival minus send time" of the last five minutes is the offset plus the shortest delivery time, which is a few
tens of milliseconds. A received message is placed at its send time converted to this station's clock. Texts
overtake file transfers on the air (normal vs bulk priority), so a photo sent just before a text usually arrives
after it; when it does, it slides up into its place while the messages below make room, and when it arrived more
than 4 s after it was sent it says so ("⏱ 18 s late", with both times in the tooltip).

**New messages.** A message that arrives while you look at another chat, is below what you are reading or hasn't
been on screen yet has a breathing outline. It counts as seen once it has been on screen for about a second (or
you scrolled past it), and the outline fades. Opening a chat with unread messages scrolls to an "N new messages"
line above them. While new messages wait below, an arrow at the bottom right shows how many and jumps to the first.

**Sound effects**, synthesised with Web Audio (no sound files, so they work offline): sending, receiving, an urgent
message, a failure, the recording beep; in UNO a card being played, drawing, your turn, UNO!, being caught, winning,
losing and a refused move; in the race the effects listed in 4.1. Browsers allow sound only after the first click
or key press on the page.

**Background music**, looping, chosen per station in Settings, with its own volume next to the effects:

| Track | What it is |
|---|---|
| Drift | slow ambient pads on four chords, with a few high notes drifting through echoes (32 s loop) |
| Carrier | quiet sixteenth-note arpeggios that echo, over a bass and a soft pad (20 s) |
| Night drive | synthwave: pulsing bass, wide chords and a soft beat, for the race (19.2 s) |
| Rain lab | rain on the window and a few bells (30 s) |
| Your music | an MP3 (or M4A, OGG, WAV) up to 40 MB, uploaded from Settings and kept on the station |

The built-in tracks are composed in `music.js` with Web Audio and rendered once into a buffer (1.5 to 3 s in a
browser without a GPU). The echoes that ring past the end are folded back onto the start, so the loop has no seam.
An uploaded file streams from the station, which answers byte-range requests so the player can loop it. The music
fades to about a tenth while a voice note is recorded or any other audio on the page plays, and it starts after
the first click or key press on the page.
## 5. Running it

### 5.0 Install (each lab PC, Ubuntu 22.04/24.04)

```bash
sudo apt install gnuradio python3-numpy          # GNU Radio 3.10+: includes grcc and gr-iio (PlutoSDR blocks)
grcc --help | head -1                            # the app compiles grc/cdma_phy.grc with this at start
python3 -c "from gnuradio import iio; print('gr-iio ok')"
pip install cryptography                         # only needed for --key (encryption)
```

There are no other dependencies: the web server is pure Python standard library, and the UI loads nothing from the
internet. Copy this whole folder to each PC and keep `grc/`, `tools/` and `wavelink/` together.

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
  2. Check the GNU Radio install: `python3 -m wavelink selftest --radio loopback`.
  3. Check the flowgraph compiles: `grcc -o build/grc grc/cdma_phy.grc` (the app does the same at start).

### 5.2 Mid-evaluation: simplex text link

```bash
# PC 2 (receiver)
python3 -m wavelink rx --id 2 --uri ip:192.168.1.10
# PC 1 (transmitter): repeats every second, spread with node 2's code
python3 -m wavelink tx --id 1 --to 2 --text "Hello from WaveLink" --uri ip:192.168.1.10
```

The receiver prints each message with its modulation/coding, symbol SNR, carrier offset and preamble metric. The
frames are BPSK with LDPC rate ½ by default (`--mcs bpsk` for uncoded BPSK, or e.g. `--mcs 16qam-3/4`). These two
small tools use the numpy PHY (a minimal GNU Radio graph: PlutoSDR source/sink plus one Python block). Their bursts
are compatible with the flowgraph PHY, so `tx` can also send to a station running `run`.

### 5.3 Final system: one station per PC

```bash
python3 -m wavelink run --id 1 --name Alice --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m wavelink run --id 2 --name Bob   --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m wavelink run --id 3 --name Carol --uri ip:192.168.1.10 --key "our-group-secret"
python3 -m wavelink run --id 4 --name Dave  --uri ip:192.168.1.10 --key "our-group-secret"
```

Each command compiles `grc/cdma_phy.grc` (you'll see `grcc` output and the PlutoSDR blocks connecting), starts the
flowgraph and the web UI. Then open **http://localhost:8080** on each PC. Stations appear within a second or two.
Node IDs are 1–4, because the flowgraph has one transmit lane per station code.

- Click a contact to chat; "Everyone" is the group.
- 📎 sends a photo or file. The mic button records a voice note of up to 15 s.
- ⚡ marks a message urgent. The # button starts tic-tac-toe.
- The gamepad icon opens the game center (UNO and the race, section 4.1).
- The bar-chart icon opens the radio dashboard.

Useful flags:

| Flag | Default | Notes |
|---|---|---|
| `--mod` | `auto` | Adaptive, or restrict the payload modulation to `bpsk`, `qpsk` or `16qam` |
| `--fec` | `auto` | LDPC code rate: adaptive, `off` (uncoded), or force `1/2`, `2/3`, `3/4`, `5/6`. Combine with `--mod` to pin one entry, e.g. `--mod 16qam --fec 3/4` |
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
python3 -m wavelink grc-sim                       # Alice, Bob, Carol on http://localhost:8081 … 8083
python3 -m wavelink grc-sim --noise 0.05 --cfo 2000 --key secret
python3 -m wavelink grc-sim --mod qpsk            # restrict the modulation
python3 -m wavelink grc-sim --noise 0.5 --fec off  # ~12 dB without FEC: compare with the default
```

This compiles and runs `cdma_phy_sim.grc`: three copies of the station PHY whose transmit signals are added and
passed through a stock Channel Model (noise, carrier offset) to every receiver. It is the same chain as on
hardware, with the ANTSDR blocks replaced by the channel. It runs at 1 MS/s (rates are a quarter of hardware),
because all three stations' Python blocks share one process.

**The numpy channel simulator** (fast, up to 4 stations, per-link impairments):

```bash
python3 -m wavelink sim --nodes 4                 # Alice..Dave on http://localhost:8081 … 8084
python3 -m wavelink sim --nodes 3 --key secret --ebn0 12
python3 -m wavelink sim --nodes 2 --mod 16qam --fec 3/4   # force modulation and code rate
```

Every link gets its own gain (±3 dB), delay, carrier offset (±2 ppm LOs at 2.5 GHz) and AWGN. The default SNR is
25 dB, so links run QPSK or 16-QAM. The SNR slider in the dashboard lets you degrade the channel live and watch
the modulation and code rate step down (16-QAM → 16-QAM ¾ → … → QPSK ½), CRC drops and retransmissions. The simulator is CPU-bound: with
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

**How the app uses the .grc.** At start, `wavelink/grc_phy.py`:

1. runs `grcc -o build/grc grc/cdma_phy.grc`, every time, so your last saved edit is what runs;
2. imports the generated `build/grc/cdma_phy.py` and creates its top block with the command-line values for the
   Parameter blocks (any parameter you delete simply keeps its default);
3. finds the Embedded Python blocks by node ID, attaches the link layer to **MAC → PHY** / **PHY → MAC**, and reads
   the dashboard data (constellation, spectrum, Es/N0, CFO, counters) from the CDMA Despreader and Burst Combiner;
4. starts the flowgraph.

**Editing it in GNU Radio Companion.** Open `grc/cdma_phy.grc` in `gnuradio-companion`. You can change variables
(gains, roll-off, `samp_rate`, `sps`), move blocks, add QT GUI sinks for your own measurements, or swap the PlutoSDR
blocks for UHD blocks; save, and the next `run` uses it. Keep in mind:

- The app needs the seven Embedded Python blocks and the `code`/`mod` PDU routing between them. The constellation
  objects must keep their point order (it is the Gray mapping shared with the Framer's symbol table). `ldpc_max_iter`
  (default 20) bounds the decoder's work per frame; `const_16qam`'s `npwr` (0.05) shapes the 16-QAM soft bits.
- The app sets the Parameter blocks, so it ignores **Run (F6)** values for those. Pressing F6 in GRC runs the PHY
  on its own: the MAC → PHY block then has no link layer, and PHY → MAC prints received frames to the console.
  That is a handy radio check, but a chat station is started with `python3 -m wavelink run`.
- Keep `grc/` inside the project folder: the Embedded Python blocks import `wavelink` from the folder above.

**Where the files come from.** `tools/make_grc.py` generates all three .grc files, with the Embedded Python block
sources in `tools/epy/`. Rerun it (`python3 tools/make_grc.py`) only if you want to regenerate them; it overwrites
edits made in GRC. `tools/render_grc.py` draws an approximate PNG of a .grc's layout without GNU Radio.

**Verification status.** All three files compile with GNU Radio 3.10.12's `grcc`, and the Embedded Python blocks'
ports resolve the way GRC's GUI sees them. `cdma_phy_sim.grc` runs end to end in GNU Radio (section 7), including
every modulation/code-rate combination, and the chat app has run on it (scenario test, 100–120 KB files). The PlutoSDR blocks of `cdma_phy.grc` have not been run against
a physical ANTSDR yet, and the files have not been opened in the GRC GUI on a lab PC.

## 6. Demo plan for the final evaluation (≈ 10 min)

0. **The PHY.** Open `grc/cdma_phy.grc` in GNU Radio Companion and walk through it: CRC-32, LDPC Encoder, the
   Framer, the five spreading lanes (Repeat × Gold code), RRC, ANTSDR sink; on receive the matched filter,
   Despreader, the three Constellation Soft Decoder branches, LDPC Decoder and CRC check. `cdma_phy_concept.grc` (F6)
   shows processing gain live.
1. **Bring-up.** Start four stations with `run` and point out the `grcc` compile in the console. Show presence and
   the PHY line (2.5 GHz, 31-chip Gold, BPSK/QPSK/16-QAM + LDPC, my code).
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
9. **Adaptive modulation and coding, big files.**
   - Send a multi-MB file Alice → Bob. The chat header shows "sending 16-QAM uncoded (258 kb/s)", and the dashboard
     constellation shows 16 clusters.
   - Start a second transfer Carol → Dave on another code. Alice→Bob moves to 16-QAM ¾ or ⅔ (not QPSK), because the
     reported SNR now includes the other link's interference and the LDPC code absorbs it. The "LDPC iterations"
     tile shows the decoder working harder.
10. **FEC on/off.** Raise `--tx-atten` until the link reports ~12–14 dB, then restart the sender with `--fec off`:
    the same file drops to BPSK at a fraction of the speed (in simulation: 4.8× slower at 14 dB).
11. **Game center.** Four-player UNO (point out that each screen shows only its own hand: the host holds the
    deck), then a race. Open the dashboard on one station during the race to show the slotted datagrams; fire a
    missile at the car ahead. Press V on one station for the chase camera.
12. **Chat touches.** Give one station a photo as its picture: the others show it after its next beacon (one
    `prof` request each). Send a photo at HD, then a text right after: the text arrives first and the photo slides
    up into its place, marked late. Start a large file and stop it from the receiver's ✕.

## 7. Tests

```bash
python3 -m tests.test_grc_flowgraph   # needs GNU Radio: compiles + runs cdma_phy_sim.grc, coded and uncoded,
                                      # unicast/broadcast, 3 stations at once, a 6 dB channel
python3 -m tests.test_grc_goodput 100 0.5   # needs GNU Radio: 100 KB through the flowgraph + MAC, FEC on vs off
python3 -m tests.test_ldpc         # LDPC encoder (all 51 lifting sizes), rate matching, AWGN curves, Golay
python3 -m tests.test_grc_cores    # the flowgraph's Python cores with the stock blocks imitated in numpy,
                                   # plus numpy-PHY <-> GRC-PHY interoperability (no GNU Radio needed)
python3 -m tests.test_modem        # PHY: impairments, false alarms, multi-user, collisions, SNR sweeps, CPU
python3 -m tests.test_profiles fast   # every modulation/code rate: SNR sweep, 2nd link, decode cost, real time
python3 -m tests.test_bigfile 1 fast 14 auto auto   # 1 MB at 14 dB with adaptive LDPC (last arg off = uncoded)
python3 -m tests.test_link         # MAC over simulated air: concurrency, priority, lossy link
python3 -m tests.test_uno          # UNO rules engine: every rule, a player leaving, 300 random games (always 108 cards)
python3 -m tests.test_games 3      # game center over the simulated air, 2-4 stations: a UNO round, a player leaving,
                                   # a race (--transfer 400: a file transfer during UNO, --key: encrypted)
python3 -m tests.test_features     # profile pictures over the air, cancelling a transfer on either end, clock offsets
python3 -m wavelink sim --nodes 3 & python3 -m tests.ui_games 3   # optional, needs Playwright: UNO, a race and a
                                   # rematch (chase camera), played with the keyboard on every station, and a chat
                                   # message sent from the race; screenshots in build/ui-shots/
python3 -m wavelink sim --nodes 2 & python3 -m tests.ui_features   # optional, needs Playwright and Pillow: themes,
                                   # wallpapers, pictures, emoji, photo/voice quality (fake microphone), cancel, a
                                   # late message, new-message arrow, sounds, buttons at the bottom, camera (fake
                                   # camera), background music, chatting during UNO: 49 checks
python3 -m tests.test_gr_backend   # numpy-PHY GNU Radio backend against a fake GR (API-shape test)
python3 -m wavelink sim --nodes 3 & python3 -m tests.scenario_api   # full app scenario through the web API
```

`tests.scenario_api` also runs against `python3 -m wavelink grc-sim` (same ports). The GNU Radio tests were run with
GNU Radio 3.10.12; the game center tests ran on the numpy simulator. The ANTSDR blocks (`iio_pluto_sink/source`)
have not been run on a physical E200 yet, so expect to tune gains in the first lab session.

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
  closer. If the spectrum is flat-topped or clipped, lower `--rx-gain`. A few header fails per burst *of another
  station* are normal: strong signals on other codes occasionally pass the preamble detector.
- **"LDPC decode failures" climbing.** Frames arrive but the code cannot correct them: the link is below the SNR its
  modulation/code rate needs. With `--fec auto` the sender steps down by itself after a few lossy windows; with a
  forced `--mod`/`--fec` it cannot. A high "LDPC iterations" average (> 8) means the link is close to the edge.
- **`rx_overflow_chunks` in the counters.** The PC can't keep up: the Despreader's input queue passed 3 s of
  samples and it dropped some. Close other programs. As a last resort lower `samp_rate` to 2e6 in the .grc on all
  stations (half the bit rate and half the CPU).
- **"Buffer too small for min_noutput_items" from a PDU to Tagged Stream block.** A burst is larger than the
  block's buffer. The shipped graph sets *Min Output Buffer* 32768 on the code lanes and the receive branches (a 1 KB
  frame at BPSK ½ is 16.5k symbols); keep it if you edit them, and raise it if you raise `max_payload`.
- **Retransmissions while two stations talk to each other at once.** Your own transmitter deafens your receiver.
  Separate the TX/RX antennas, lower TX power, or raise `--hd-defer`.
- **The microphone doesn't work.** Use `http://localhost`; browsers block the microphone on plain-HTTP remote
  addresses. You can attach an audio file instead.

## 9. Limitations and improvements (for the report)

- **FEC: what is and isn't there.** LDPC with rate-compatible puncturing and hybrid ARQ type I. Not implemented:
  incremental redundancy (HARQ type II: on a failure, send *more parity* of the same codeword instead of the whole
  frame again — the 5G circular buffer already allows it via redundancy versions 1–3), soft combining of
  retransmissions, and codes for frames longer than 1,056 bytes (several codewords).
- **Detection limits the low end.** Coded QPSK ½ would decode at ~1.5 dB, but the 48-symbol preamble is only
  detected reliably from ~4 dB. A 96-symbol preamble (`preamble_bits`) would gain ~3 dB at the cost of airtime and
  detector CPU.
- **16-QAM under interference.** 31-chip spreading suppresses another equal-power link by about 15 dB. Uncoded
  16-QAM needs ~20 dB, 16-QAM ¾ ~11 dB, so with LDPC the link stays on 16-QAM; with three or more strong
  simultaneous links it steps further down.
- **Throughput ceiling.** About 236 kb/s goodput per link at 4 MS/s in the numpy simulator (uncoded 16-QAM on a
  clean link), limited by Python DSP CPU (not yet measured on hardware with the GRC PHY). A C++ OOT correlator and
  LDPC decoder would allow higher chip rates; 64-QAM ⅚ would need ~19 dB.
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
- **Game center.** The host station is a single point of failure for both games: if it goes off the air, the room
  closes (UNO could hand the deck to another station). Reloading the page during a race puts your car back on the
  grid. Race datagrams share the broadcast code with beacons, which they occasionally collide with; presence
  therefore counts any frame heard. On the GNU Radio PHY a game message can't overtake file frames already in its
  code lane (up to 0.6 s); a priority input on the Combiner would fix that. There is no spectator mode, and no
  computer players. Not yet tested on `grc-sim` or the ANTSDRs.
- **Message order.** The clock-offset estimate includes the shortest delivery time (tens of milliseconds) and
  follows a clock that is reset only after the old samples age out (5 minutes), so for a few minutes after a
  station's clock jumps its messages can be placed by up to the jump. Running NTP on the lab PCs avoids it.
- **Pictures and settings.** A profile photo is sent to a station that asks for it, but there is no way to push a
  new photo before its next beacon (up to 5 s). The wallpaper stays on its station. Voice-note re-encoding needs
  WebCodecs (Chrome, Edge, Firefox 130+); other browsers fall back to re-recording the clip in real time.

## 10. Suggested split for a group of four

| Member | Owns |
|---|---|
| A | PHY DSP and coding: `codes.py`, `modem.py`, `ldpc.py`, `golay.py`, BER/FER measurements, hardware gain tuning |
| B | GNU Radio and hardware: `grc/cdma_phy.grc`, `grc_phy.py`, ANTSDR firmware/IPs, `tx`/`rx` mid-eval demo |
| C | Link layer: `mac.py`, ARQ, adaptive modulation-and-coding table, priority tests, the simulator |
| D | Application and UI: `node.py`, `web/`, `games/` (game center), encryption, dashboard, demo script |

## Project layout

```
wavelink/
  config.py      PHY parameters, address → code mapping
  codes.py       Gold codes, preamble patterns, RRC taps
  modem.py       burst modulator + streaming multi-code demodulator (Golay header, LDPC payload)
  ldpc.py        5G NR LDPC: lifting, encoder, rate matching, interleaver, layered min-sum decoder, demapper
  nr_bg1.py      5G NR base graph 1 table (3GPP TS 38.212), generated by tools/gen_nr_bg1.py
  golay.py       extended Golay (24,12) header code, soft ML decoding
  grc_phy.py     compiles + loads the .grc, cores of its Embedded Python blocks, GrcPhyRadio (MAC <-> flowgraph)
  radio_base.py  TxMixer (multi-code transmit), radio interface, dashboard data
  radio_gr.py    numpy-PHY GNU Radio graph (--phy numpy, tx/rx/selftest): ANTSDR via gr-iio or UHD, loopback
  radio_sim.py   multi-node channel simulator (numpy PHY)
  mac.py         link layer (ARQ, adaptive modulation-and-coding table)
  crypto.py      AES-256-GCM
  node.py        application layer
  games/         game center: hub.py (rooms, invitations, clock sync, UNO host, race slots), uno.py (rules
                 engine), race.py (race datagram format)
  web/           server.py + static/: index.html, app.js, style.css (chat); settings.js (profile, themes,
                 wallpapers), media.js (quality sheets, recorder, camera, WebM muxer), emojipick.js + emoji.js
                 (palette), sfx.js (sound effects), music.js (background music), gcchat.js (chat in the
                 game center), wavelink-*.png (logo, mark, icons), Montserrat (wordmark);
                 games.js, uno.js, race.js, games.css (game center); bundled fonts' licence FONTS-OFL.txt
  __main__.py    CLI: run | grc-sim | sim | tx | rx | selftest
grc/
  cdma_phy.grc          the station PHY (ANTSDR E200) used by `run`
  cdma_phy_sim.grc      three station PHYs + channel model, used by `grc-sim`
  cdma_phy_concept.grc  stock-block DS-CDMA demonstration with GUI plots
build/grc/       grcc output, regenerated at every start (not shipped)
assets/          the team logo: wavelink-logo-source.png (as designed), wavelink-logo(-dark).png (transparent)
tools/
  make_grc.py    generates the .grc files
  epy/           source of the seven Embedded Python blocks
  gen_nr_bg1.py  builds wavelink/nr_bg1.py from Sionna's table, cross-checked against AFF3CT
  render_grc.py  approximate PNG of a .grc layout (no GNU Radio needed)
  make_logo.py   cuts the team logo out of its background: web/static/wavelink-*.png and assets/
  gen_emoji.py   builds web/static/emoji.js (the palette's emoji and names) from Unicode data
tests/           LDPC, GRC flowgraph, GRC goodput, GRC cores + interop, modem, profiles, big file, link,
                 GR backend, API scenario, UNO rules, game center (simulated air), game center in the browser,
                 station features, chat features in the browser
```
