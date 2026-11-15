<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/wavelink-logo-dark.png">
    <img src="assets/wavelink-logo.png" alt="WaveLink" width="280">
  </picture>
</p>

# WaveLink Chat — multi-node DS-CDMA messaging over ANTSDR E200 + GNU Radio

By team **WaveLink** for the Communication Design Project (2026). A WhatsApp-style chat for 2–4 stations. Every
station is one PC with one ANTSDR E200. All stations share **2.5 GHz** using **direct-sequence CDMA** (31-chip Gold
codes) with **BPSK, QPSK or 16-QAM** payloads protected by **LDPC forward error correction** (the 5G NR code, rates ½
to ⅚, chosen per link). The stations take turns in **time slots**, and in its slot a station reaches every other
station at once, one code each (**TD-CDMA**, section 3); the original spread-ALOHA access, every link on the air
whenever it likes, is still there with `--mac aloha`.

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
 │ Link layer     mac.py         │  addressing, fragmentation, selective-repeat ARQ, adaptive
 │                tdma.py        │  modulation + coding, priority; TD-CDMA time slots (default),
 │                               │  group ACKs, relaying; or spread ALOHA
 ├────────── MAC -> PHY ─────────┤  ← Embedded Python blocks in the .grc (grc_phy.py)
 │ Physical layer                │
 │  grc/cdma_phy.grc             │  GNU Radio flowgraph: CRC-32, LDPC encoder, framing, Gold-code
 │  (compiled with grcc at start)│  spreading lanes, RRC, ANTSDR (Pluto) sink/source, despreading,
 │                               │  constellation soft decoders, LDPC decoder, CRC check
 └───────────────────────────────┘
```

## 1. Requirements checklist (from the project brief)

| Brief requirement                               | Where / how                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Mid-evaluation**: simplex text TX → RX | `python -m wavelink tx …` and `python -m wavelink rx …` (section 5.2)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Digital modulation                              | DS-CDMA with 31-chip Gold codes, RRC α = 0.35. BPSK preamble and Golay-coded header; payload **BPSK, QPSK or 16-QAM** with **LDPC rate ½, ⅔, ¾, ⅚ or uncoded**, chosen per link from the receiver's measured SNR (adaptive modulation and coding)                                                                                                                                                                                                                                                                                                    |
| GNU Radio based                                 | The PHY is `grc/cdma_phy.grc`, built from stock GNU Radio blocks plus seven Embedded Python blocks; the app compiles and runs it (section 5.5)                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Two transceivers, real two-way link             | ANTSDR E200 on each PC; every station sends in its own time slot to all the others at once (TD-CDMA), or whenever it likes (spread ALOHA); ACKs flow on the reverse link                                                                                                                                                                                                                                                                                                                                                                                              |
| Packetization and reassembly                    | 1,009-byte fragments (`fast`), up to 65,535 per message (≈ 66 MB) — `mac.py`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Unique addressing                               | Node ID 1–4 = its Gold code (PHY: one code lane per station in the flowgraph) + `dst` byte checked in every frame (MAC)                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| CRC error detection                             | CRC-8 on the PHY header; CRC-32 on every frame (stock **Stream CRC32** blocks in the flowgraph) — bad frames are dropped                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Error correction (extra)                        | **LDPC** (3GPP 5G NR base graph 1) on every payload, soft-decision min-sum decoding; extended **Golay (24,12)** on the header. Up to **4.8× goodput** on a 14 dB link, ~9 dB coding gain at rate ½ (section 2.2)                                                                                                                                                                                                                                                                                                                                  |
| ACK-based reliable delivery                     | Selective-repeat ARQ: cumulative + selective (SACK) acknowledgements, fast loss detection, timeouts, duplicate detection. With TD-CDMA the ACKs ride in the slot headers and a group message is acknowledged by every member                                                                                                                                                                                                                                                                                                                                          |
| Payloads: text, images, audio                   | Text with an emoji palette; photos at a chosen quality (480, 640 or 1280 px WebP/JPEG, or the original); voice notes or audio files at a chosen quality (Opus 12 or 24 kb/s, or as recorded); plus any file, each cancellable from either end                                                                                                                                                                                                                                                                                                                         |
| Application                                     | Chat with 1:1 chats, an "Everyone" group, delivery and read ticks, messages in the order they were sent, new-message highlights, profile pictures shared over the air, per-station themes, wallpapers and sounds (section 4.2), tic-tac-toe, and a **game center** with **UNO** and a real-time **street race with combat pickups**, each for 2–4 stations (section 4.1)                                                                                                                                                                           |
| **Bonus**: encryption                     | `--key <passphrase>` → AES-256-GCM, scrypt KDF, addresses authenticated as AAD                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| **Bonus**: priority handling              | 4 classes (control/ACK > urgent ⚡ > normal > bulk media); urgent text overtakes an image in the sender's next slot                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| **Bonus**: graphical dashboard            | Radio panel: constellation, spectrum, Eb/N0, chip SNR, CFO, counters, queues, links; the TD-CDMA slot plan, timing (TX → RX offset, clock drift, late bursts), the relay switch and routes, group-message ACKs per member                                                                                                                                                                                                                                                                                                                                            |
| Other additions                                 | **Transmit power control** against the near-far problem: every frame at the power its receiver needs, closed loop through ACKs and beacons (section 3.4); adaptive modulation and coding with SNR feedback in ACKs (hybrid ARQ type I); 10 MB+ file transfer; multi-code transmission to several stations at once; spread-ALOHA reception of colliding same-code bursts; presence beacons; channel simulator; GNU Radio Companion flowgraphs                                                                                                                    |
| Medium access (extra)                           | **TD-CDMA** (default): a time slot per station, kept in radio samples (TX → RX offset measured from the station's own bursts), in which it sends to every other station at once on their codes; no near-far problem, no deaf receivers, ACKs in the slot headers, slots sized on demand, joining, leaving, lead handover, duplicate-address warning (section 3.1). **Group messages** as one broadcast acknowledged by every member. **Relaying** through a station that hears both, switchable with `--relay` or on the dashboard (section 3.2) |

## 2. Physical layer

| Parameter                                      | `grc/cdma_phy.grc` (default PHY)                                              | numpy PHY `robust` profile |
| ---------------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------- |
| Carrier                                        | 2.500 GHz (`--freq`)                                                          | same                         |
| Sample rate / RF bandwidth                     | 4 MS/s / 4 MHz                                                                  | same                         |
| Chip rate                                      | 2 Mchip/s (2 samples/chip)                                                      | 1 Mchip/s (4 samples/chip)   |
| Spreading                                      | 31-chip Gold codes, processing gain 14.9 dB                                     | same                         |
| Symbol rate                                    | 64.5 ksymbol/s                                                                  | 32 ksymbol/s                 |
| Payload bit rate, uncoded BPSK / QPSK / 16-QAM | **64.5 / 129 / 258 kb/s**                                                 | 32 / 65 / 129 kb/s           |
| Payload bit rate with LDPC                     | 32 kb/s (BPSK ½) … **215 kb/s (16-QAM ⅚)**                             | half                         |
| Forward error correction                       | LDPC, 5G NR base graph 1, rate ½ ⅔ ¾ ⅚ (or uncoded), one codeword per frame | same                         |
| Max frame payload                              | 1,024 bytes (MAC frame + CRC-32)                                                | 512 bytes                    |
| Pulse shape                                    | Root-raised cosine, α = 0.35, 8-chip span                                      | same                         |

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

| Block                  | Kind            | Job                                                                                                                                                                                                                                                                                                                            |
| ---------------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| MAC → PHY, PHY → MAC | Embedded Python | Hand frames between the app and the flowgraph as PDUs (metadata: `code`, `mod` = modulation and coding, e.g. `16qam-3/4`, `pwr` = power backoff, `at` = the chip the burst must start on; on RX also SNR, CFO, LDPC iterations and the frame's receive sample index)                                                 |
| LDPC Encoder           | Embedded Python | 5G NR LDPC encoding of frame + CRC-32, rate matching to the requested rate, 5G bit interleaver (section 2.2)                                                                                                                                                                                                                   |
| CDMA PHY Framer        | Embedded Python | Builds the burst as constellation **indices**: preamble, Golay-coded header, coded payload bits → symbol numbers in `sym_table` (0–1 BPSK, 2–5 QPSK, 6–21 16-QAM)                                                                                                                                                  |
| CDMA Burst Combiner    | Embedded Python | Adds the 5 code lanes chip by chip (several stations served at once) and sends zeros between bursts, so the ANTSDR always has samples. A burst tagged with `at` starts exactly on that chip (time slots); one that arrives after its chip is dropped and counted                                                             |
| CDMA Despreader        | Embedded Python | Synchronisation: preamble correlation on this node's code and the broadcast code (with TD-CDMA only where the slot plan says bursts can start), CFO estimate, fractional-delay despreading with DLL timing, Golay header decode, carrier tracking. Outputs the corrected payload symbols with the burst's receive sample index |
| LDPC Decoder           | Embedded Python | Undoes interleaving and rate matching, layered normalised min-sum decoding with early stop; uncoded frames get hard decisions                                                                                                                                                                                                  |
| Everything else        | Stock GNU Radio | CRC-32, spreading (Repeat × Gold code), Chunks to Symbols, pulse shaping, ANTSDR sink/source, matched filter, **Constellation Soft Decoder** with `variable_constellation` objects (soft bits for the LDPC decoder), length-tag handling                                                                              |

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

| Rung                                     | Info bits/symbol | `fast` profile | Needs Es/N0                     |
| ---------------------------------------- | ---------------- | ---------------- | ------------------------------- |
| QPSK ½ (also ACKs, beacons, broadcasts) | 1.0              | 65 kb/s          | — (floor; works from ~4–5 dB) |
| QPSK ¾                                  | 1.5              | 97 kb/s          | 6.0 dB                          |
| QPSK ⅚                                  | 1.67             | 108 kb/s         | 7.0 dB                          |
| 16-QAM ½                                | 2.0              | 129 kb/s         | 9.5 dB                          |
| 16-QAM ⅔                                | 2.67             | 172 kb/s         | 11.0 dB                         |
| 16-QAM ¾                                | 3.0              | 194 kb/s         | 12.5 dB                         |
| 16-QAM ⅚                                | 3.33             | 215 kb/s         | 14.0 dB                         |
| 16-QAM uncoded                           | 4.0              | 258 kb/s         | 22.5 dB                         |

It drops one rung for 10 s whenever windows (with TD-CDMA: every 8 frames the ACK records account for) keep losing
more than 20 % of their frames, and messages of one or two fragments go two rungs lower (a lost frame costs a whole
round trip), but with TD-CDMA no lower than the slot allows: a full fragment at the slow rung (43.9 ms at 16-QAM ¾)
can be longer than an idle slot (44 ms with two stations, less with more), and the message would wait ~1.5 s for its
slot to grow; it takes the slowest rung that fits instead. Interference counts (spread ALOHA): with another equal-power link on the channel the measured SNR falls
to ~12–15 dB, and the link moves from uncoded 16-QAM to 16-QAM ¾ or ⅔ instead of dropping to QPSK. `--mod bpsk|qpsk|16qam` restricts the modulation, `--fec off|1/2|2/3|3/4|5/6` the coding (`--fec off` gives the old uncoded
BPSK/QPSK/16-QAM behaviour). ACKs use QPSK ½: the airtime of uncoded BPSK with ~9 dB more margin.

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

Single-link speeds below are measured with transmit power control off (one link alone has no neighbour to
protect; with it on, a strong link is turned down to ~25.5 dB at its receiver, still uncoded 16-QAM, section 3.4).

On the GRC PHY, running `grc/cdma_phy_sim.grc` in GNU Radio 3.10.12 (`tests.test_grc_flowgraph`, `grc-sim`), with
the spread-ALOHA link layer (before TD-CDMA):

| Test                                                                                                  | Result                                                                                                                     |
| ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Lone frames: uncoded BPSK/16-QAM and LDPC QPSK ½ ¾, 16-QAM ⅔ ¾ ⅚, BPSK ½; unicast and broadcast | 10/10 delivered through the stock soft decoders, LDPC decoder and CRC-32 check                                             |
| Three stations transmitting at the same instant on one carrier                                        | 3/3 delivered, **including 16-QAM ½ at 12 dB** (each link sees the other two as noise; uncoded 16-QAM needs ~20 dB) |
| Noisy channel, Es/N0 ≈ 6 dB                                                                          | QPSK ½ and ¾ delivered; uncoded QPSK and 16-QAM ½ lost, as expected                                                     |
| 100 KB file, Es/N0 ≈ 12 dB, MAC + ARQ (`tests.test_grc_goodput`)                                   | **37.8 kb/s with adaptive LDPC (16-QAM ⅔) vs 15.6 kb/s uncoded (BPSK): 2.4×**, 0 retransmissions                   |
| 100 KB file, clean channel (≈ 46 dB)                                                                 | 48 kb/s adaptive (mostly uncoded 16-QAM, ¾ after a loss) vs 34 kb/s with `--fec off`                                    |
| Full app scenario (`grc-sim` + `tests.scenario_api`)                                              | passed: text, group, image, game, read receipts                                                                            |

The simulated graph runs at 1 MS/s because all three stations' Python blocks share one process. On hardware each
station has its own PC and runs the 4 MS/s rates above.

On the numpy PHY (`tests.test_ldpc`, `tests.test_modem`, `tests.test_profiles`, `tests.test_bigfile`; the file
transfers with spread ALOHA, for TD-CDMA see section 3.5):

| Test                                                        | Result                                                                                                                                          |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| LDPC alone, BPSK on AWGN, 1 KB frames                       | rate ½: 0/20 frame errors at **Eb/N0 1.5 dB**; ¾ at 2.5 dB; ⅚ at 3.5 dB (uncoded needs ~10.5 dB)                                       |
| Full receiver, 1 KB frames, symbol SNR for ~100 % delivery  | QPSK ½ ~5 dB (detection-limited), QPSK ¾ 5.5, QPSK ⅚ 6, 16-QAM ½ 9, ⅔ 10, ¾ 11, ⅚ 12.5, uncoded 16-QAM 20–22 dB                         |
| Second equal-power link on another code (Es/N0 20 dB)       | uncoded 16-QAM 2/4 frames; **16-QAM ¾ and ⅚ 4/4**                                                                                       |
| QPSK ½, 200-byte frames, CFO ±12 kHz, 20 ppm clock offset | 10/12 at Es/N0 4 dB, 11/12 at 6 dB (uncoded BPSK, same bit rate: 6/12 at 8 dB, 12/12 at 10 dB)                                                  |
| Implementation SNR ceiling (receiver accuracy)              | about 44 dB for every modulation                                                                                                                |
| 3 s of pure noise on two codes                              | 0 detections, 0 frames                                                                                                                          |
| Two simultaneous links, interferer +10 dB (BPSK)            | both decoded (near-far tolerance)                                                                                                               |
| Two senders → same receiver, overlapping                   | 20/20 bursts decoded                                                                                                                            |
| Receive + decode cost per 1 KB frame (incl. LDPC)           | 27–97 ms for 33–128 ms on air; one receiver keeps up with continuous maximum-rate traffic of every rung (1.1–1.6× real time on a 2-core VM) |
| **2 MB file, `fast`, Es/N0 28 dB, adaptive**        | **67.7 s, 236 kb/s goodput, 0 retransmissions** (uncoded 16-QAM)                                                                          |
| **1 MB file, Es/N0 14 dB**                            | **176 kb/s with LDPC (16-QAM ¾) vs 37 kb/s uncoded (BPSK): 4.8×**, 0 retransmissions either way                                         |
| 10 MB file, 28 dB (before FEC was added; same rung)         | 340 s (5 min 40 s), 235 kb/s goodput, intact, 0 retransmissions                                                                                 |

## 3. Link layer (MAC)

Two ways to share the air, chosen with `--mac` (every station must use the same one):

|                                                  | **TD-CDMA** (default, `--mac tdcdma`)                                                                                                                  | Spread ALOHA (`--mac aloha`, the earlier scheme)                     |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Who transmits when                               | One station at a time, each in its own **time slot** of a repeating cycle; in its slot it sends to all the others at once, one Gold code per destination | Every station whenever it likes, on the receivers' codes               |
| Near-far problem                                 | None: only one transmitter is on the air                                                                                                                       | Handled by transmit power control (3.4)                                |
| A station's own transmitter deafens its receiver | Doesn't matter: nobody else transmits during your slot                                                                                                         | Pauses after every ACK request (the *ACK gap*), half-duplex deferral |
| ACKs                                             | Records in each station's next slot header                                                                                                                     | Separate ACK frames, sent at once                                      |
| Group messages                                   | One broadcast, acknowledged by every member                                                                                                                    | One unicast copy per member                                            |
| Relaying                                         | Yes, switchable (3.2)                                                                                                                                          | No                                                                     |

### 3.1 TD-CDMA time slots (`wavelink/tdma.py`)

```
   one cycle: 100 ms while every station is idle; 213.5 / 333.5 / 415.5 ms measured with 2 / 3 / 4 all sending
 ┌──────────────┬───────────────────────────────────┬──────────────┬───────────┐
 │ lead (Alice) │ Bob, data queued: 101 ms          │ Carol, idle  │ join slot │ next cycle ...
 │ idle: 15 ms  │ (3 frames of uncoded 16-QAM)      │ 15 ms        │ 11.5 ms   │
 └──────────────┴───────────────────────────────────┴──────────────┴───────────┘
   every slot starts with its owner's slot header on the broadcast code (QPSK ½, 4.6–9.6 ms);
   at the same instant the owner's bursts for each destination start on that destination's code:
     code 0  │header│race state│beacon│group data ...│
     code 1  │frame for Alice│frame for Alice│frame for Alice│
     code 3  │frame for Carol│frame for Carol│
   0.5 ms guard between slots; the plan is counted in ticks of 0.25 ms
```

- **The lead.** The first station on the air leads: its header (the first slot of the cycle) carries the cycle
  number and the slot plan. A starting station listens for 1.2 s, plus 0.25 s for every address above 1, so of
  stations switched on together the lowest address leads; a station that hears a lead joins it (the incumbent
  keeps leading). When two networks meet, the lower lead address wins and the other network joins it.
- **Joining.** A newcomer sends its header in the *join slot*, at a random point of its first 4 ms and in a random
  half of the cycles, so two newcomers rarely collide (and if they do, their code phases usually still separate
  them). The lead adds it to the plan.
- **Plan changes** (a station joins, leaves or wants more or less airtime) are announced in the lead's header at
  least 1 s and 4 cycles before they take effect, so every station knows the plan before it has to transmit by it.
  One change at a time, and every lead header repeats the plan in force. So a station that heard the lead's plan in
  cycle *L* knows the plan of every cycle up to *L* + *w* (*w* = 4 cycles or 1 s of them, whichever is more: 10 at
  100 ms, counted in the newest plan's cycles; when a change it has heard is still to come, from that change's first
  cycle, since the lead decides no further change before it), and it sends in a later cycle only after hearing the
  plan again: it **holds its slot** rather than follow a stale plan into somebody else's (`tests/test_edge.py` 14: a
  station that misses every announcement holds at each change and never overlaps anyone). Why 1 s: on the GNU Radio flowgraph a burst is built 0.2–0.6 s before it leaves
  the antenna (TX buffers ~0.1 s plus the lead time), and a plan repeated by a relaying member is that much older
  again. With 0.5 s a hidden station never got to send its join request (found with fake ANTSDRs, section 7).
- **Slot sizes on demand.** Each header says how much airtime its owner wants. Idle: 15 ms (the header, and small
  messages on the other codes beside it). With data queued: about 100 ms, as a whole number of the frames it is
  sending (3 × 32.5 ms of uncoded 16-QAM, 3 × 39 ms of 16-QAM ⅚, 1 × 126 ms of QPSK ½; at most 250 ms). A slot grows
  at the next plan change, with 2 ms of headroom, and shrinks only after its owner has wanted at least 5 ms less for
  1 s, so a header a few bytes longer or shorter doesn't change the plan. A cycle shorter than 100 ms gets
  the spare time spread over its slots: 10 headers per station per second are plenty, keep the receivers' CPU load
  down, and leave room in an idle slot for a beacon or a short message without asking for more (while every wish
  fits into those padded slots the plan is not changed at all). A cycle longer than 700 ms gets its data slots
  shortened in proportion.
- **Leaving.** A station whose headers stop for 6 cycles and 1.5 s loses its slot (another member reporting that
  it hears the station counts too); back on the air, it asks for a slot in every join slot and has one at the next
  plan change. If the *lead* goes silent for 2 cycles, the next lowest address takes over (the cycle numbering and
  the timing carry on; a station further down the list takes over only while the ones before it are silent too),
  but only if it still hears other stations (hearing nobody, it is the one cut off) and none of their headers says
  it heard the lead in the last 3 cycles (then only it has lost the lead: it marks its header *lead unheard*, and
  the others repeat the lead's plan for it, section 3.2). A follower that hears no lead and no repeated plan for 3 s
  starts over.
- **Headers fit their slot.** The plan sections always go in; the rest (timing feedback, link table, ACK records)
  as far as there is room, and what is left out goes in the next slot.
- **Duplicate addresses.** Every header carries a random 16-bit nonce chosen at start-up. Two nonces under one
  address (the lead gets a join request from an address already in the plan, or any station sees them alternate)
  are announced in the headers, and the stations concerned show *Another station is using address N: give every
  station its own --id* on the dashboard.

**Timing in samples, not in PC time.** Slots are kept in sample counts of the radio, where the timing is exact;
the PC's clock and its buffering jitter (tens of milliseconds) never enter it.

```
 the lead's header leaves its antenna ─► reaches Bob: preamble found at receive sample j   (±0.25 sample)
 Bob: cycle n started at  j − (lead's slot start + ½ guard)              (in his receive-sample count)
 Bob's own slot, receive side:  s = cycle start + his slot start + ½ guard
 Bob transmits it at transmit sample  k = s − D
     D = (receive index of my own burst) − (transmit sample I put it at)        the TX → RX offset
```

- **D, the TX → RX offset.** TX and RX of one ANTSDR run from one sample clock, so a station's own burst always
  reaches its own receiver the same number of samples after it was put on the transmit stream. D contains the TX
  buffers, the driver, the kernel buffers, the RX buffers and the arbitrary start of the two counters, and none of
  them needs to be known. A station hears its own transmitter (the TX leaks into the RX), and every slot header
  carries the transmit sample it was put at, so D is measured in every slot: from the header's contents the first
  time, afterwards from where the preamble shows up (no decoding needed). It changes only after a TX underrun or a
  lost RX buffer; a new value is believed once two measurements agree. A radio-side underrun or overflow, or an own
  header that doesn't come back, makes the receiver search everywhere for 1.5 s to find it again.
- **Quiet probes while D is unknown or in doubt.** A newcomer doesn't know D before it has heard itself, and a guess
  is tens of ms off on the radios (their RX buffers can't be seen), which would put its first join request over the
  next slot. So its first transmissions are probes: its header on its own code (no other station despreads it), 20
  dB down (a station that never hears them joins on the guess). Likewise a member whose TX stream underran, or whose
  own header turned up somewhere new, sends only probes until it has measured D again, in at most 4 of its slots (a
  PC too busy to hear itself then sends by the D it has rather than lose its slot). The probes overlap other slots,
  but 20 dB down on a code nobody listens to. (Found with fake ANTSDRs: section 7.)
- **If a station can't hear itself**, the others steer it: every header lists, for each station heard, how many
  samples off its header arrived against where the plan puts it (timing feedback), and a station whose own headers
  don't come back moves D by 0.7 × the median report until they agree.
- **Clock drift** between stations doesn't accumulate: every lead header re-anchors the followers, and they also
  measure the ratio of their clock to the lead's (shown in ppm on the dashboard) to bridge missed headers.
- **The guard** of 0.5 ms covers the RRC tails, the detection error, drift between headers (50 ppm × a 400 ms
  cycle = 20 µs) and propagation (3 m = 10 ns; the simulator gives every link a random delay of up to 100 µs to test
  it).

**Measured delays and slot arrangement** (the simulators of this project, and the ANTSDR E200 facts they rest on):

| Quantity                                                               | Value                                                                                                                                                                                          | Source                                                                                                                             |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Preamble timing error (receive index of a burst)                       | ≈ 0.25 sample RMS (worst seen 0.6) at Es/N0 ≥ 6 dB                                                                                                                                           | numpy simulator, `tests/test_modem.py`                                                                                           |
| TX → RX offset D of a station                                         | constant to the sample over 10 minutes with clocks 50 ppm apart; jumps by exactly a TX underrun's gap, and by minus a lost RX buffer                                                           | `tests/test_tdma_phy.py`, `tests/test_edge.py` 5, 6                                                                            |
| Handing a burst to the flowgraph → it reaches the Burst Combiner      | 62 ms idle, 70–102 ms loaded (lanes, LDPC Encoder and Framer are message-driven)                                                                                                              | GNU Radio simulator (GNU Radio 3.10.12)                                                                                            |
| Burst Combiner → antenna                                              | the TX buffers: libiio keeps 4 kernel buffers + the one being filled, 5 × 65,536 samples at 4 MS/s ≈ 82 ms (all of it inside D)                                                              | libiio default of 4 kernel buffers (Analog Devices wiki, *libiio tips & tricks*); ANTSDR E200 with its Pluto-compatible firmware |
| TX underruns by TX buffer size: 16 k / 32 k / 64 k / 128 k samples     | 27 / 13 / 0 / 0 per run → the default `--tx-buffer` is now 65,536                                                                                                                           | GNU Radio simulator                                                                                                                |
| How early a slot's bursts are handed over (before their first sample)  | numpy simulator 4 ms; numpy PHY on an ANTSDR one TX buffer + 12 ms; flowgraph 150 ms to start with, adapting: +30 ms after a late burst, −5 ms per second while > 80 ms are left, 120–450 ms | `radio_base.py`, `grc_phy.py`                                                                                                  |
| Signal-to-interference between codes of one transmitter (31-chip Gold) | 1 / 2 / 3 codes at once: 39.9 / 29.4 / 25.3 dB (uncoded 16-QAM needs 22.5)                                                                                                                     | numpy simulator                                                                                                                    |
| Airtime of a 1 KB frame (4 MS/s, 2 Mchip/s)                            | 16-QAM 32.5 ms, 16-QAM ⅚ 39 ms, QPSK ½ 126 ms                                                                                                                                                | `modem.py`                                                                                                                       |
| Slot header                                                            | 21–61 bytes, 4.6–9.6 ms; a lead header announcing a plan change in a 4-station network 106 bytes, 15 ms                                                                                      | numpy simulator                                                                                                                    |
| Cycle with 2 / 3 / 4 stations                                          | idle: 100 / 100 / 99.5 ms (slots 44 / 29.5 / 21.5 ms + join slot 11.5 ms); all sending: 213.5 / 333.5 / 415.5 ms (data slots 101 ms, the 3-station lead's 120 ms: its link ran 16-QAM ⅚)      | numpy simulator                                                                                                                    |
| Receiver search with slot-aware windows                                | 9.5 / 11.3 / 16 % of the samples (2 / 3 / 4 idle stations) instead of 100 %                                                                                                                    | numpy simulator                                                                                                                    |

The ANTSDR E200 (MicroPhase, Crowd Supply page): AD9361 (70 MHz – 6 GHz, up to 56 MHz bandwidth) or AD9363
(325 MHz – 3.8 GHz, 20 MHz) transceiver, 2 × 2, 200 kS/s – 61.44 MS/s, Xilinx Zynq-7020 with 512 MB DDR3, Gigabit
Ethernet, a 40 MHz VCXO rated ±2 ppm and a PPS / 10 MHz reference input, Pluto-compatible (libiio) firmware in the
QSPI flash and UHD firmware on the micro-SD card. ±2 ppm means two boards' sample clocks differ by at most 4 ppm,
1.6 µs over a 400 ms cycle; the tests use up to 50 ppm. A shared PPS / 10 MHz reference would remove even that, but
nothing here needs it.

**What goes into a slot.** The header first, on the broadcast code at QPSK ½; at the same instant, on each
destination's code, the frames for that station (datagrams, frames passed on as a relay, then messages by priority).
After the header on the broadcast code: the newest race state, a beacon every 5 s, group data. Every burst follows
the previous one on its code with no gap, and everything in the slot goes at **one power, equal on every code**:
what the station that needs most power needs (everybody must decode the header), and with more than two codes in the
slot 10·log10(codes / 2) dB less, so the sum stays at the level of two full-power codes (RMS 0.35 of DAC full scale,
9 dB of headroom for the peaks; the numpy PHY counts clipped samples). No near-far problem is left: the codes from
one transmitter reach a receiver through one path, and nothing else is on the air.

**The receiver searches only where bursts can start** (`Demodulator.set_windows`): around every other station's
slot start (± 0.27 ms), right after every burst it has found on a code (the next one follows back to back), and the
whole join slot on the broadcast code. Every 8th cycle, while the lead's header is missing, and for 1.5 s after a
possible timing slip it searches everywhere. Its own slot it doesn't search, except around its own header, which it
recognises by where it arrives. In the simulator three idle stations went from 3.2 to 1.2 s of CPU per second of
air; on the ANTSDR PCs the despreader block saves the same share.

**Reliability.** No ACK frames and no pauses. A station acknowledges every message it got pieces of in each of its
next two slot headers: an *ACK record* (sender, acker, msg_id, fragments, cumulative base, the fragment it heard
last, Es/N0, a SACK bitmap of up to 512 fragments beyond the base). A fragment sent before the one heard last and
still missing is known lost and goes again in the sender's next slot; a timeout (3 cycles + 2 hand-over times

+ 0.1 s, or 2.5 × the measured round trip + a cycle if that is longer) resends what is in flight. Losses that keep
  coming step the link's modulation down for 10 s, as with ALOHA (section 2.2). A big transfer streams in every slot,
  up to 496 fragments ahead of the first one not yet acknowledged.

**Group messages** go out **once**, on the broadcast code, at the rate the weakest member can take. Every member
acknowledges in its own header; the sender keeps a set per member and resends (as a broadcast) whatever any member
misses. The chat shows a tick per member (*2/3* on the bubble, names in its tooltip, progress per member on the
dashboard). A member that has left the network is dropped from the message; one that never answers is given up
after 4 timeouts, and the app sends it the message on its own (through a relay if there is one). A member the
sender reaches only through a relay gets that copy right away.

### 3.2 Relaying (`--relay on|off`, the dashboard switch, `POST /api/relay {"on": true}`)

Every slot header carries a *link table*: how its station hears each other one (Es/N0 at full power). From these a
station knows who hears whom. When a destination doesn't hear us (we are missing from its table) or our messages to
it keep timing out, and a station with relaying on hears us and the destination at 7 dB or more (from the
destination's table if it reaches us, otherwise from the relay's), messages go through that station, **one hop**:

```
 Alice ──FWD on Bob's code, Alice's slot──► Bob (relay) ──FWD on Carol's code, Bob's slot──► Carol
 Alice ◄── Bob's header passes on Carol's ACK records ◄── Carol's header
```

- The relay sends what it passes on in its own slot, at the rate of the last hop; up to 256 frames per destination
  wait there, each fragment once: a copy the sender repeats while the first one still waits (its ACK timer is
  shorter than the relay's slot takes to grow) is dropped. For a relayed message the sender counts losses from the
  first transmission of the frame the destination heard last, and a timeout does not step its rate down (the
  frames are mostly still queued at the relay). Back to the direct path once it has been good (≥ 5 dB, no timeouts) for 5 s. A station drops out of
  the link tables 3 s after its last header, so a station that has gone is not thought reachable through a relay.
- A station that can't hear the *lead* keeps time through another member. Every member that hears the lead repeats
  the lead's plan in its header (whether relaying is on or off: this is only timing) while it hears a station that
  isn't synchronised to the lead (a newcomer, or a lone network of its own), that times its slots by repeated plans,
  or whose header says *lead unheard*; it also passes on that station's join requests and airtime requests, and the
  lead counts a station another member hears as present.
- "via Bob" appears on relayed messages at both ends, and a station reached only through a relay shows as online,
  with "via" in its contact. Beacons are not relayed, so a relaying station's own beacon lists the names and
  picture tags of the stations it hears that a neighbour can't (`"v"`; at most 200 bytes of JSON per beacon, long
  names shortened first): the far station shows with its name and picture, and its photo is fetched through the
  relay. The dashboard lists the routes in use and the frames waiting.
- Switched off at a station, it neither relays messages for others nor uses relays (it still repeats the slot plan
  for stations that can't hear the lead); the others see it in its next header and stop routing through it. Frames
  lost on the way are resent once a route is back: a relayed transfer simply resumes when relaying is switched on
  again (`tests/test_edge.py` 9).

### 3.3 Frames

```
DATA   | type | flags | dst | src | msg_id(2) | frag_idx(2) | frag_count(2) | payload ≤1009             | CRC-32 |
FWD    | type | flags | dst | src | msg_id(2) | relay | frag_idx(2) | frag_count(2) | payload ≤1009     | CRC-32 |
SLOT   | type | flags | 255 | src | cycle(2)  | tx_sample(4) nonce(2) status want_ms lead lead_seen(2)     |
       |   sections: PLAN · NEXT (lead) · ACK records · LINK table · TFB timing · DUP · JREQ (repeat)  | CRC-32 |
ACK    | type | flags | dst | src | msg_id(2) | frag_count(2) | base(2) | trigger(2) | snr(1) | SACK   | CRC-32 |  ALOHA
BEACON | type | flags | 255 | src | n         | n × (addr, Es/N0 heard) | {"n": name, "e": key-id, …}   | CRC-32 |
DGRAM  | type | flags | dst | src | 0         | payload (game clock sync, race car states)            | CRC-32 |
flags: bits 0–1 priority, bit 2 ACK-request (ALOHA), bits 3–7 this frame's transmit power backoff in dB
```

- **Addressing.** Frames whose `dst` is neither me nor 255 are dropped and never acknowledged; a relay takes the
  `FWD` frames that name it as the relay. Frames whose `src` is me are dropped too, except my own slot headers,
  which measure D.
- **Fragmentation.** 1,009-byte fragments (`fast` profile; one byte less than before, so every fragment also fits
  a `FWD` frame), up to 65,535 per message (≈ 66 MB).
- **Priority.** In a slot each code serves its messages in priority order (control > urgent ⚡ > normal > bulk
  media), so an urgent text overtakes a 10 MB transfer in the sender's next slot.
- **Presence.** Every frame heard counts. With TD-CDMA that is a slot header from every station every cycle, so the
  game center notices a station gone after 3 s. Beacons (name, key id, avatar, clock) every 5 s; a contact shows
  offline after 17.5 s without a frame from it.
- **Datagrams** (`DGRAM`) are sent once, never acknowledged, at QPSK ½. With TD-CDMA they go into the next own slot;
  the race asks for the newest car state each time a slot is built, so exactly one fresh state goes out per cycle.

**Spread ALOHA details** (`--mac aloha`): pipelined selective repeat; the sender sets ACK-request on every *W*-th
frame (*W* ≈ 0.5 s on air) and pauses after it (the *ACK gap*, 0.15 s on hardware) so the receiver can reply without
transmitting over incoming frames, which its own transmitter would swamp; ACKs go at once, on the sender's code;
fragments sent before the triggering one and missing are resent at once; a timeout (0.35 s in the simulator, 0.5 s on
hardware) resends everything in flight after a random back-off; 30 failures in a row with no progress fail the
message; a station holds back a new window while a burst for it is arriving (0.25 s). Group messages are one
unicast copy per member, in parallel on their codes.

With either scheme, together with the LDPC code this is **hybrid ARQ type I**: the code corrects most errors, the
CRC-32 catches what it can't, ARQ resends those frames, and adaptive modulation and coding (section 2.2) keeps that
rare.

### 3.4 Transmit power control (the near-far problem)

With TD-CDMA only one station transmits at a time, so there is no near-far problem to solve. Power control there
sets the one level of each slot (3.1): what the station that needs most power needs, aimed at 25.5 dB, so a station
next to you arrives at ~25 dB instead of 40–50 dB (receivers less overdriven, and less noise for the other systems
in the lab's 2.5 GHz band). For spread ALOHA it is what makes the scheme work at all; the rest of this section
describes it.

In this system a station's Gold code belongs to the *receiver*: frames for Dave go on code 4, whoever sends them.
The near-far problem is therefore not two stations sending to the same receiver (that is a same-code collision,
which ARQ handles), but this: **Alice talks to Carol right next to her at full power, and at Dave, also next to her,
Alice's code-3 signal drowns Bob's weak code-4 signal from across the room.** Despreading leaves an interferer's
signal at the level of its *chip*-level SNR (its despread Es/N0 minus the 14.9 dB processing gain), so every 1 dB
that Alice arrives above what Carol needs is 1 dB more noise for Bob's link at Dave.

So every frame goes at the lowest power its receiver needs:

| Step                                            | How                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Each frame says its power                       | Bits 3–7 of the flags byte carry the frame's backoff (0–31 dB). The receiver adds it to the Es/N0 it measures, so it knows each path **at full power** whatever power was used.                                                                                                                                                                                                                                                                                |
| The receiver reports                            | In every ACK (`snr` byte) and in its beacons (an entry per station heard: address, Es/N0 at full power), so a station learns how every other one hears it, with or without traffic between them.                                                                                                                                                                                                                                                                     |
| The sender backs off                            | To a **target of 25.5 dB** at the receiver: what the fastest allowed modulation needs (uncoded 16-QAM, 22.5 dB) plus 3 dB, so only power the link cannot use is taken away and it keeps its modulation. Power goes up at once and down only in steps of 2 dB or more; back to full power if a station's reports stop for 60 s; at most 30 dB of backoff. The backoff is worked out first, then the modulation for the Es/N0 the receiver will see at that power. |
| Power from the path, rate from the interference | The reports are SINR: they drop while something else is on the air. The power follows the **path**, the best recent report (falling 0.5 dB/s), so a burst of interference doesn't make the sender louder: louder is more noise for whoever caused it, who would turn up in turn, a power race back to full power. The modulation follows the latest reports, so under interference a link steps its rate down instead.                                           |
| Outer loop                                      | A link that keeps losing frames while backed off (two lossy windows within 3 s) although its receiver reports no interference aims 3 dB higher, up to +12 dB, and 1 dB lower again per 30 s without losses. That covers radio impairments and fading the Es/N0 estimate doesn't show; the worst case is full power, what the link did without power control.                                                                                                           |
| Broadcasts and beacons                          | Group messages, race car states and beacons go at the power the weakest station on the air needs; a station that hasn't reported yet counts as needing full power, so a newcomer finds us. Everything on the broadcast code goes at one level, because the receiver rejects a burst much weaker than one that just started on the same code as a sidelobe of it.                                                                                                       |
| Applying it                                     | Digitally, per burst: the numpy PHY scales the burst; in the flowgraph, the *MAC → PHY* block puts `pwr` in the PDU metadata, the LDPC Encoder and Framer keep it, *PDU to Tagged Stream* turns it into a tag on the burst's first chip, and the *Burst Combiner* scales the burst. `--tx-atten` (the ANTSDR's attenuator) sets the ceiling.                                                                                                                |

Why 25.5 dB and not lower: a target such as 17 dB (16-QAM ⅚ + 3 dB) protects weak neighbours more, but puts every
strong link on coded modulation: slower, an LDPC decode per frame, and more time on air for the same data, which is
more receiver CPU at every station (section 8, RX overflows). The first version aimed at 17 dB; on three ANTSDRs
that version gave QPSK instead of 16-QAM, low rates and scattered constellations. `--pc-target 17` still sets it;
`--power-control off` sends everything at full power.

With power control on, the constellation on the dashboard is less tight than at full power. That is the point: a
station next to you now arrives at ~25.5 dB instead of 40–50 dB, still 3 dB more than uncoded 16-QAM needs.

Measured with spread ALOHA on the simulated air (`tests/test_power.py`): Alice, Carol and Dave together (paths 24 dB
stronger than to Bob), Alice sending a long file to Carol while Bob sends 40 KB to Dave (four runs):

|                                                            | Bob's 40 KB to Dave   | Bob's frames that get through |
| ---------------------------------------------------------- | --------------------- | ----------------------------- |
| Alice silent                                               | 1.6–2.6 s            | 93–100 %                     |
| Alice sending, power control off                           | not delivered (fails) | 0 %                           |
| Alice sending, power control on (Alice at −16 to −20 dB) | 4.6–9.1 s            | 31–57 %                      |

And where no station is much nearer than the others, power control must not cost speed. The GNU Radio flowgraph
(`grc-sim`, spread ALOHA, three stations, `--noise 0.05`, every link at 25–31 dB) before power control and with it:

|                      | 60 KB alone  | two 60 KB at once | three 150 KB at once | LDPC failures (whole run) |
| -------------------- | ------------ | ----------------- | -------------------- | ------------------------- |
| before power control | 12.1–13.6 s | 13.2–16.5 s      | 64.9–66.4 s         | 95                        |
| power control on     | 13.1–13.6 s | 13.2–17.8 s      | 54.1–58.1 s         | 98                        |

The dashboard's Links table shows, per station, the Es/N0 we hear and the power it sent at, the Es/N0 it hears us
at, and **our power** to it; the *Transmit power control* panel has a switch to turn it off and on live. To see the
near-far problem in the simulator, use spread ALOHA and put stations next to each other:

```bash
python3 -m wavelink sim --nodes 4 --mac aloha --link-gain 1:3=24 --link-gain 1:4=24 --link-gain 3:4=24
```

### 3.5 TD-CDMA or spread ALOHA? (measured)

`tests/test_edge.py` on the numpy simulator, both schemes on the same air (ranges over two or three runs):

| Scenario                                                                                                                                                             | TD-CDMA                                                                     | Spread ALOHA                                                   |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | -------------------------------------------------------------- |
| 4 stations each send 100 KB to the next, all at once; a station hears its own transmitter 25 dB above the others (as on the ANTSDRs), ALOHA with its hardware pauses | **173–181 kb/s together** (48–66 each), 6–24 retransmissions       | 53–58 kb/s together (13–19 each), 755–900 retransmissions   |
| The same with idealised radios: own transmitter only 3 dB above the others, so a station receives while it sends                                                     | 160–180 kb/s (47–66 each), 6–10 retransmissions                          | **294–321 kb/s** (78–94 each), 14–38 retransmissions  |
| Near-far, power control off: a pair side by side (+20 dB) sends 80 KB while a far station (−6 dB) sends 30 KB to the same receiver                                  | far station **65–68 kb/s, 0 retransmissions**; the pair 87–114 kb/s | far station 43–44 kb/s, 16 retransmissions; the pair 206 kb/s |
| One 2 MB transfer between 2 stations, 28 dB                                                                                                                          | 190 kb/s, 0 retransmissions                                                 | 236 kb/s (section 2)                                           |
| Two stations that can't hear each other                                                                                                                              | through a relay (a file at 45–51 kb/s)                                     | not reachable                                                  |

Where a station can receive while it transmits, ALOHA keeps all four links on the air at once and wins: that is CDMA
doing what it was made for. On a real radio a station's own transmitter, on the same frequency, arrives at its own
receiver tens of dB above anything else; while it sends it is deaf. ALOHA then needs its pauses and still loses the
frames that arrive while their receiver is sending (755–900 retransmissions), while TD-CDMA never transmits over
anyone's reception. One link alone gets about 80 % of ALOHA's speed under TD-CDMA: the receiver's own slot, the join
slot and the headers take their share of every cycle. Every TD-CDMA sender gets the same airtime, so their rates
differ only by the modulation their links run (slowest / fastest 0.69–0.82); with ALOHA the shares depend on who
collides with whom.

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
- **Group "Everyone":** with TD-CDMA, **one broadcast** on code 0 that every online member acknowledges in its own
  slot header (section 3.1). While some members still miss it the bubble shows how many have it (*2/3*), the tooltip
  who; a member that never acknowledges gets the message on its own, and a member reached only through a relay gets
  a relayed copy straight away. With spread ALOHA, a separate reliable unicast to every online station, each on its
  own code and all in parallel.
- **Ticks:**
  | Tick       | Meaning                                                                |
  | ---------- | ---------------------------------------------------------------------- |
  | 🕒         | queued                                                                 |
  | ✓         | on the air (ALOHA: the first window)                                   |
  | ✓✓       | acknowledged (a group message: by every member)                        |
  | *2/3* ✓ | a group message some members have acknowledged so far                  |
  | blue ✓✓  | the recipient opened the chat (read receipt)                           |
  | !          | failed after retries                                                   |
  | ⊘         | cancelled (by you, or "cancelled by …" when the recipient stopped it) |
- **Encryption:** AES-256-GCM, with the key = scrypt(passphrase). Each message uses a random 96-bit nonce, and
  `(src, dst)` is the associated data. The UI shows a short key-ID, and warns when a peer's beacon advertises a
  different key.
- **History:** stored in `data/node<N>/history.jsonl`, with media files in `data/node<N>/media/`.

### 4.1 Game center (`wavelink/games/`)

The gamepad button at the top of the chat list opens the game center. It has two games, each for 2, 3 or 4
stations, played with the keyboard:

|             | UNO                                                                            | Gold Code GP                                                                                 |
| ----------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Game        | The card game, standard rules (no house rules)                                 | A top-down street race through a rain-soaked night city, 2, 3 or 5 laps, with combat pickups |
| Who decides | The host's station: it shuffles, deals, keeps every hand and checks every move | Each station drives its own car. A hit is decided by the station of the car that was hit     |
| On the air  | Reliable game messages (ARQ), one view per player per move                     | One unacknowledged datagram per car per cycle, in its station's slot, on the broadcast code  |
| Look        | Plum velvet table with brass trim, original card faces set in Lilita One       | Sodium street lamps and neon on wet asphalt in the rain, Chakra Petch HUD                    |

**Rooms.** Pick players on a game's poster and press *Invite*. The others get an invitation (a toast, a dot on the
gamepad icon and a card in the game center) and press *Join*; the host starts once at least one has joined (stations
that haven't answered stay out). Invitations, answers, the lobby, the start and the end go as `gc` messages:
reliable, sent at high priority so they overtake chat and file traffic, and not stored in the chat history. A player
who leaves is announced and the game goes on without them. **Chat** in the game center's top bar (with the number of
unread messages) opens a panel at the side: pick a chat, read, and send, while the game goes on behind it. Keys typed
in the panel never reach the game (so typing "d" doesn't draw a UNO card); in the race your car rolls on without
steering while you type, and the other stations keep hearing it. <kbd>Esc</kbd> closes the panel. The **+** button
beside the panel's message box opens a tray with **Voice** (a voice note, recorded in the panel), **Camera**,
**File**, **Emoji** and **Priority** (the next message, photo, file or voice note goes out urgent); whatever they
send goes to the chat open in the panel, and their sheets (photo quality, camera) open over the game while it keeps
running. **Radio** in the same bar opens the radio dashboard (live tiles, constellation, spectrum, links, queue) as a
panel on the left, over the game; on a phone the dashboard and the chat panel take turns at the bottom of the screen.
Keys go to the game unless they are meant for something on top of it (a text box, the chat panel, the dashboard or a
sheet). Closing the game center (<kbd>Esc</kbd>) keeps a running game going behind the chat; the gamepad icon brings
it back.

**Leaving and coming back.** A player whose station goes off the air during a game (no slot header heard for 3 s with
TD-CDMA, no frame for 17.5 s with spread ALOHA) is *away* for up to 60 s. UNO pauses for everybody with a countdown,
and the host keeps the hand; the race goes on, the car stays on the track as a frozen ghost. A player heard again in
time carries on where they were; later, the seat is dropped and the player, when back, is told the game went on
without them. If the **host** goes off the air, the next player in seat order takes over the room and the game goes
on: the host sends a copy of the UNO table with every player's view (kept in the station, never shown in the
browser), and the race clock carries over. The old host, back, plays on as a player: every takeover raises the room's
host number, carried by every view and room message, so the claims of a host that lost the connection and doesn't
know yet are recognised as older and it steps down.

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
  station 20 times a second. The station keeps the last one (with the lap start, best lap and event counter), so a
  page reloaded mid-race opens straight into the race and the car carries on where it was, laps and pickup kept.
  An event id counts as seen for 10 s only, so ids that wrap at 256 or restart after a reload still get through.
- **On the air.** The station puts the newest state on the air as a 28-byte datagram (+13 bytes per event; with one
  event a 52-byte frame, 7.9 ms at QPSK ½). With TD-CDMA it rides in the station's own slot, right after the slot
  header: one fresh state per car per cycle, 10 a second while nothing else is going on (a 100 ms cycle) and fewer
  while a file transfer stretches the cycle (4.7 a second at 213 ms, 2.4 at 415 ms). With spread ALOHA the race makes
  time slots of its own on the broadcast code: seat *k* of *N* transmits at *S₀ + k·P/N + m·P* on the host's radio
  clock, with *P* = max(1/15 s, *N* × airtime × 1.7), so the cars' bursts never overlap and no station is
  transmitting (deaf) while another car's update is on the air: 15 updates a second with 2, 3 or 4 cars (with
  `--key` the frame is 80 bytes, 11.4 ms: still 15 a second for 2–3 cars, 12.9 for four).
- **Clocks.** The countdown, the timestamps and finish times use the host's clock. Each other player measures its
  offset with datagram pings, keeping the sample with the shortest round trip; the host can only start the race once
  every player is synchronised. With spread ALOHA the same pings synchronise the radio clock the race's slots use.
  On the ANTSDRs that is the same clock; in the simulator it is the simulated air's sample counter, which falls
  behind real time when the PC can't keep up, so there a burst is handed to the simulated radio up to 20 ms early
  with its exact start sample.
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

**Measured** (`tests/test_games.py`, numpy simulator, Es/N0 25 dB; TD-CDMA unless marked):

|                                                     | 2 stations                       | 3 stations                                                            | 4 stations                             |
| --------------------------------------------------- | -------------------------------- | --------------------------------------------------------------------- | -------------------------------------- |
| UNO: a move until every screen shows it             | median 0.16 s, 90 % 0.18–0.21 s | median 0.22–0.24 s, 90 % 0.31–0.32 s (spread ALOHA: 0.22 s, 0.34 s) | median 0.40–0.41 s, 90 % 0.53–0.58 s |
| UNO while a 400 KB file goes to one player          |                                  | median 0.29–0.33 s, 90 % 0.45–0.59 s                                |                                        |
| Race: updates heard per car per second              | 10.0                             | 6.1–6.45 (spread ALOHA: 6.4)                                         | 3.5–3.7                               |
| Race: state age when heard, median                  | 0.06–0.08 s                     | 0.07–0.12 s                                                          | 0.10–0.17 s                           |
| Race: packets lost, events missed, duplicates shown | none, none, none                 | none, none, none                                                      | none, none, none                       |
| Simulator: CPU seconds per second of air            | 0.7                              | 1.6                                                                   | 2.7                                    |

One PC computes every receiver in the simulator, and above one CPU second per second of air the simulated air runs
slower than real time. That, not the slots, limits the race update rate there with 3 and 4 stations; on hardware
every car is heard once per cycle, 10 times a second while nobody sends a file. Every UNO view also carries the copy
of the table that a host handover needs, so views are larger than they were before handovers existed.
A UNO round (and the deal of the next one), a full race and a rematch with the chase camera were also
played through the browser UI on the numpy simulator with 2, 3 and 4 stations (`tests/ui_games.py`, keyboard input
in headless Chromium; with spread ALOHA, and again with 2 stations on TD-CDMA: 10–11 car updates heard per second);
no remote car was ever drawn beyond the rails. A race page drew 60 frames per second (the
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

| Setting          | Choices                                                                                                                                                                                                                                                                | Kept                                                                                      |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Name and picture | your initial or an emoji on a colour of your choice, or a photo (cropped square, 160 px WebP, about 5 KB)                                                                                                                                                              | `data/node<N>/profile.json`; goes to the other stations over the air                    |
| Theme            | eight palettes (WaveLink, the default, from the logo's navy, blue and cyan; Lagoon, Saffron, Orchid, Cobalt, Coral, Graphite, Neon) or a palette made from any colour; light, dark or the system's choice; breathing lights on or off                                  | profile.json, plus a copy in the browser so the first paint already has the right colours |
| Chat wallpaper   | seven patterns drawn in CSS from the palette (plain, Gold-code chips, QAM constellation, waves, rain, dusk, velvet) or your own photo, cropped, turned and adjusted in the wallpaper editor, with a fit (crop to fit, fit inside, stretch to fit, normal) and a dimmer | this station only (`data/node<N>/wallpaper.*`, and the original as `wallsrc.*`)       |
| Sounds           | sound effects on or off, volume; background music (four built-in loops or your own MP3) with its own volume                                                                                                                                                            | profile.json; an uploaded MP3 stays on this station (`data/node<N>/music.*`)            |
| Photos and audio | choose the quality each time, or always send at a default                                                                                                                                                                                                              | profile.json                                                                              |
| Gold Code GP     | north-up or chase camera                                                                                                                                                                                                                                               | profile.json                                                                              |
| Buttons          | Game center, Radio and Settings next to your name, or in a bar with their names at the bottom of the chat list                                                                                                                                                         | profile.json                                                                              |

Each station keeps its own settings, so two people at two stations can have different looks.

**The wallpaper editor.** Uploading a photo for the chat's background (or *Crop, rotate, adjust…* later) opens it
next to a preview of the chat window with a few bubbles over it:

| Tab    | What it does                                                                                                                                                                                                                                                                                                                                                                                       |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Crop   | The frame starts in the shape of your chat window (measured as it will be with Settings closed), as large as the picture allows, so the picture fills the chat without being cut again. Drag it to move it, drag a corner to resize it (the shape is kept), scroll or pinch to zoom; the arrow keys and + / − work too. *Free* lets the frame take any shape, *Whole picture* keeps all of it |
| Rotate | A quarter turn left or right and a mirror; the frame keeps the same part of the picture                                                                                                                                                                                                                                                                                                            |
| Adjust | Brightness, contrast, saturation, blur (the same strength on any size of picture) and the dimming                                                                                                                                                                                                                                                                                                  |
| Fit    | What happens when the chat window isn't the frame's shape, which changes with the window: **crop to fit** (fills it, cutting off the edges that don't fit), **fit inside** (all of it, with bands of the chat colour), **stretch to fit** (squeezed to the window's shape), **normal** (actual size, in the middle). Also in Settings                                      |

The page makes the picture itself (on a canvas, at most 2560 px, WebP) and the station keeps it with the original
and the editor's settings, so the editor always starts again from the original: a crop can be undone later. A new
photo replaces both. (Uploaded wallpapers used to be shown at their actual size: the `#wall` element's own
`background` shorthand had been overriding *cover*.)

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

|                        | What                      | Where                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Breathing (slow, idle) | presence and readiness    | the logo's halo, online dots and the ring of light round an online contact's picture, unread badges, the send button once there is text, the line beside the open chat, your picture while your radio hears frames                                                                                                                                                                                            |
| Pulsing (an event)     | something went on the air | the logo sends out a radio ring when frames are heard or a message comes or goes (at most every 1.2 s), and so does the light in the Radio panel; a light passes over each new bubble; your bubble glows while it is on its way and stops when it is delivered; new messages breathe until seen; the header's signal line flows while a file goes to or from that chat; the new-message arrow sends out rings |

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

|         | Photo                                                                           | Voice note or audio file                                                                                                                                                                                                                                                                                                                                                                          |
| ------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Choices | Saver (480 px), Standard (640 px), HD (1280 px), Original                       | Low (Opus 12 kb/s), Standard (Opus 24 kb/s), High (as recorded, 48 kb/s) or Original for a file                                                                                                                                                                                                                                                                                                   |
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
"arrival minus send time" of the last five minutes is the offset plus the shortest delivery time, which is a few tens
of milliseconds (with TD-CDMA up to about one cycle, ~0.1 s: a message waits for its sender's slot). A received
message is placed at its send time converted to this station's clock. Texts overtake file transfers on the air
(normal vs bulk priority), so a photo sent just before a text usually arrives after it; when it does, it slides up
into its place while the messages below make room, and when it arrived more than 4 s after it was sent it says so ("⏱
18 s late", with both times in the tooltip).

**New messages.** A message that arrives while you look at another chat, is below what you are reading or hasn't
been on screen yet has a breathing outline. It counts as seen once it has been on screen for about a second (or
you scrolled past it), and the outline fades. Opening a chat with unread messages scrolls to an "N new messages"
line above them. While new messages wait below, an arrow at the bottom right shows how many and jumps to the first.

**Sound effects**, synthesised with Web Audio (no sound files, so they work offline): sending, receiving, an urgent
message, a failure, the recording beep; in UNO a card being played, drawing, your turn, UNO!, being caught, winning,
losing and a refused move; in the race the effects listed in 4.1. Browsers allow sound only after the first click
or key press on the page.

**Background music**, looping, chosen per station in Settings, with its own volume next to the effects:

| Track       | What it is                                                                                  |
| ----------- | ------------------------------------------------------------------------------------------- |
| Drift       | slow ambient pads on four chords, with a few high notes drifting through echoes (32 s loop) |
| Carrier     | quiet sixteenth-note arpeggios that echo, over a bass and a soft pad (20 s)                 |
| Night drive | synthwave: pulsing bass, wide chords and a soft beat, for the race (19.2 s)                 |
| Rain lab    | rain on the window and a few bells (30 s)                                                   |
| Your music  | an MP3 (or M4A, OGG, WAV) up to 40 MB, uploaded from Settings and kept on the station       |

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
flowgraph and the web UI. Then open **http://localhost:8080** on each PC. The first station on the air starts the
time slots after listening for 1.2 s; the others join it within a few cycles and appear in the contact list. Node IDs
are 1–4, because the flowgraph has one transmit lane per station code; every station needs its own.

- Click a contact to chat; "Everyone" is the group.
- 📎 sends a photo or file. The mic button records a voice note of up to 15 s.
- ⚡ marks a message urgent. The # button starts tic-tac-toe.
- The gamepad icon opens the game center (UNO and the race, section 4.1).
- The bar-chart icon opens the radio dashboard (in the game center: the Radio button).

Useful flags:

| Flag                | Default              | Notes                                                                                                                                                                                                        |
| ------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--mod`           | `auto`             | Adaptive, or restrict the payload modulation to `bpsk`, `qpsk` or `16qam`                                                                                                                              |
| `--fec`           | `auto`             | LDPC code rate: adaptive, `off` (uncoded), or force `1/2`, `2/3`, `3/4`, `5/6`. Combine with `--mod` to pin one entry, e.g. `--mod 16qam --fec 3/4`                                            |
| `--uri`           | `ip:192.168.1.10`  | The ANTSDR's libiio address (flowgraph parameter *ANTSDR URI*)                                                                                                                                             |
| `--tx-atten`      | 10 dB                | 0 = full power (flowgraph parameter *TX attenuation*)                                                                                                                                                      |
| `--rx-gain`       | 40 dB                | Raise for distance; lower if the dashboard spectrum is clipping (*RX gain*)                                                                                                                                |
| `--freq`          | 2.5 GHz              | Every station must match (*Carrier*)                                                                                                                                                                       |
| `--mac`           | `tdcdma`           | Medium access: `tdcdma` = time slots (section 3.1), `aloha` = the earlier spread ALOHA. Every station must use the same                                                                                  |
| `--relay`         | `on`               | TD-CDMA: pass on messages for stations that can't hear each other, and use relays (section 3.2); also a switch on the dashboard                                                                              |
| `--tx-buffer`     | 65536                | ANTSDR TX buffer in samples (16 ms; no underruns measured in the GNU Radio simulator). Raise it if the dashboard shows TX underruns; it only changes the TX → RX offset, which TD-CDMA measures             |
| `--ack-gap`       | 0.15 s               | Spread ALOHA: minimum pause after an ACK request, so the receiver can answer while the sender is silent. The link layer stretches it automatically to the measured round-trip time.                          |
| `--hd-defer`      | 0.25 s               | Spread ALOHA: holds new transmissions while a burst for us is arriving, to limit self-interference                                                                                                           |
| `--power-control` | `on`               | Transmit power control (section 3.4); `off` = every frame at full power                                                                                                                                    |
| `--pc-target`     | 25.5 dB              | Es/N0 a receiver should hear us at (the fastest modulation + 3 dB); 17 protects weak neighbours more but makes strong links coded and slower                                                                 |
| `--host 0.0.0.0`  | —                   | Lets other PCs view the UI. The microphone only works on `localhost`.                                                                                                                                      |
| `--grc FILE`      | `grc/cdma_phy.grc` | Run a different (e.g. edited copy of the) flowgraph                                                                                                                                                          |
| `--phy numpy`     | `grc`              | Fallback PHY without the .grc: the receiver and transmitter in numpy inside a minimal GNU Radio graph. Only this PHY uses `--radio uhd`, `--profile`, `--rate`, `--sps`, `--gold`, `--gain-mode` |

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
python3 -m wavelink sim --nodes 4 --mac aloha --link-gain 1:3=24 --link-gain 1:4=24 --link-gain 3:4=24  # near-far (3.4)
python3 -m wavelink sim --nodes 3 --cut 1:3         # Alice and Carol out of each other's range: relayed through Bob
python3 -m wavelink sim --nodes 3 --ppm 2=40 --ppm 3=-35   # sample clocks off by 40 and -35 ppm: the slots hold
```

Every link gets its own gain (±3 dB, or set with `--link-gain A:B=dB`), delay, carrier offset (±2 ppm LOs at 2.5 GHz)
and AWGN. The default SNR is 25 dB, so links run QPSK or 16-QAM. The SNR slider in the dashboard lets you degrade the
channel live and watch the modulation and code rate step down (16-QAM → 16-QAM ¾ → … → QPSK ½), CRC drops and
retransmissions. The simulator is CPU-bound: with several stations busy it runs slower than real time, but its clock
is simulated, so throughput figures stay correct. `--self-leak` (3 dB) is how much stronger a station hears its own
transmitter than a 0 dB link: the simulator lets a station receive while it sends, which real radios can't (section
3.5); `--self-leak 25` is closer to an ANTSDR.

**Fake ANTSDRs: the real flowgraph, one process per station, radios emulated** (`wavelink/fake_antsdr.py`):

```bash
python3 -m wavelink fake-air                      # the air between the stations (keep it running)
python3 -m wavelink run --id 1 --name Alice --uri fake: --port 8081 --key secret   # one terminal per station,
python3 -m wavelink run --id 2 --name Bob   --uri fake: --port 8082 --key secret   # exactly as on the lab PCs
python3 -m wavelink run --id 3 --name Carol --uri fake: --port 8083 --key secret
python3 -m wavelink fake-air --cut 1:3 --ppm 2=20 --speed 0.5 --control 8790   # hidden station, clock error,
                                                  # half speed for a slow PC, a control port for live changes
```

With a `fake:` URI the app swaps the flowgraph's two ANTSDR blocks for stand-ins with the same arguments and setters
that behave like gr-iio's towards GNU Radio: the sink takes whole `tx_buffer` buffers into a queue of 4 (libiio's
default) that the DAC drains at 4 MS/s and blocks while it is full; the source hands out whole 32,768-sample buffers;
both clip at full scale; an empty TX queue sends zeros (underrun) and RX buffers not fetched in time are lost
(overflow), so the streams slip exactly as on a slow PC. The air plays every DAC and fills every ADC on the PC's
clock: per-link gain, delay, phase and LO offset, a station's own transmitter 25 dB above the others, AWGN
(`--esn0`, 28 dB on a 0 dB link), and on request clock errors, interference, cut links and injected faults. Each
station needs 1.1–2.1 cores of a 2.8 GHz Xeon per second of air (section 9); on a PC too small for all of them,
`--speed 0.5` runs the air (and the stations' timers, which follow its sample clock) at half speed. The control port takes JSON
lines: `{"cmd": "cut", "a": 1, "b": 3, "on": true}`, `{"cmd": "off", "slot": 2}`, `{"cmd": "esn0", "db": 10}`,
`{"cmd": "txgap", "slot": 2, "n": 6000}`, `{"cmd": "status"}`.

The dashboard's mode line shows which PHY a station runs: "GNU Radio PHY · ANTSDR", "GNU Radio PHY · simulated
channel" or "simulation (numpy PHY)".

### 5.5 The GNU Radio Companion files (`grc/`)

| File                     | What it is                                                                                                                                                                                                                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cdma_phy.grc`         | **The station's physical layer** on an ANTSDR E200 (Pluto/libiio firmware). Used by `run`. Parameters: Node ID, ANTSDR URI, carrier, RX gain, TX attenuation, TX buffer.                                                                                                            |
| `cdma_phy_sim.grc`     | Three copies of the same PHY (nodes 1–3) joined by a Channel Model with noise and carrier-offset variables. Used by `grc-sim`.                                                                                                                                                           |
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

**Verification status.** Before TD-CDMA, all three files compiled with GNU Radio 3.10.12's `grcc`, the Embedded Python
blocks' ports resolved the way GRC's GUI sees them, `cdma_phy_sim.grc` ran end to end in GNU Radio (section 7),
including every modulation/code-rate combination, and the chat app ran on it (scenario test, 100–120 KB files). The
files were since regenerated for TD-CDMA (start-chip tags, receive sample indices, TX buffer 65,536); they compile
with GNU Radio 3.10.9.2's `grcc`, and `cdma_phy.grc` has run the TD-CDMA edge cases with emulated ANTSDRs, one
process per station (section 7). The PlutoSDR blocks of `cdma_phy.grc` have not been run against a physical ANTSDR
yet, and the files have not been opened in the GRC GUI on a lab PC.

## 6. Demo plan for the final evaluation (≈ 10 min)

0. **The PHY.** Open `grc/cdma_phy.grc` in GNU Radio Companion and walk through it: CRC-32, LDPC Encoder, the
   Framer, the five spreading lanes (Repeat × Gold code), RRC, ANTSDR sink; on receive the matched filter,
   Despreader, the three Constellation Soft Decoder branches, LDPC Decoder and CRC check. `cdma_phy_concept.grc` (F6)
   shows processing gain live.
1. **Bring-up.** Start four stations with `run` and point out the `grcc` compile in the console. Show presence and
   the PHY line (2.5 GHz, 31-chip Gold, BPSK/QPSK/16-QAM + LDPC, my code). Open the dashboard's **Time slots** panel:
   the slot plan (the lead first, then the others, then the join slot), the TX → RX offset D every station measured
   from its own bursts, and its clock against the lead's in ppm.
2. **Text plus ACK.** Alice → Bob. Ticks go 🕒 → ✓ → ✓✓ (the ACK rode in Bob's next slot header), then blue when Bob
   opens the chat. Point to Eb/N0 and CFO in the chat header.
3. **Addressing.** Carol's dashboard shows no frames for the Alice→Bob traffic, because it is on code #2. Run
   `tx --to 2` from a PC while Carol runs `rx --id 3`: nothing is printed.
4. **CDMA inside a slot.** Alice sends a photo to Bob and a voice note to Carol at the same time, while Dave sends
   a file to Alice. Alice's slot carries both of her transfers at once on codes #2 and #3; the slots of the stations
   with data grow to ~100 ms on the dashboard and shrink back a second after. Nobody transmits in another's slot:
   no ACK pauses, no near-far.
5. **CRC plus ARQ.** Move an antenna away, or raise `--tx-atten`. The dashboard shows CRC-32 drops and
   retransmissions while the image still arrives intact.
6. **Priority.** Start a large image, then send an ⚡ urgent text. It arrives first.
7. **Encryption.** Restart one station with a different `--key`. The UI warns, and its messages can't be opened.
8. **Application.** Play a game of tic-tac-toe across the room.
9. **Adaptive modulation and coding, big files.** Send a multi-MB file Alice → Bob. The chat header shows
   "sending 16-QAM uncoded (258 kb/s)" and the constellation 16 clusters. Move Bob's antenna away step by step: the
   link moves to 16-QAM ¾, ⅔, … and the "LDPC iterations" tile shows the decoder working harder.
10. **FEC on/off.** With the link at ~12–14 dB, restart the sender with `--fec off`: the same file drops to BPSK at a
    fraction of the speed (in simulation: 4.8× slower at 14 dB).
11. **Group message.** Send a photo to Everyone: one broadcast; the bubble counts *1/3*, *2/3*, then ✓✓, and the
    dashboard shows every member's progress.
12. **Relaying.** Put Carol where Alice can't hear her (another room, or her antennas in a metal box): her contact
    shows "via Bob" and Alice's messages to her go through Bob, marked "via Bob" at both ends. Turn relaying off on
    Bob's dashboard: they wait; on again: they continue.
13. **Leaving.** Close Dave's app: his slot is freed after 3 cycles. Close the lead's app during a transfer: the next
    station takes over within half a second and the transfer goes on. Start it again: it joins as a member.
14. **Game center.** Four-player UNO (point out that each screen shows only its own hand: the host holds the
    deck), then a race; the car states ride in every station's slot. Take one station off the air for 10 s
    (disconnect its antennas): it is *away*, UNO pauses with a countdown and the race shows its car as a ghost, and
    it carries on when it is back. Press V on one station for the chase camera.
15. **Chat touches.** Give one station a photo as its picture: the others show it after its next beacon (one
    `prof` request each). Send a photo at HD, then a text right after: the text arrives first and the photo slides
    up into its place, marked late. Start a large file and stop it from the receiver's ✕.

## 7. Tests

```bash
python3 -m tests.test_grc_flowgraph   # needs GNU Radio: compiles + runs cdma_phy_sim.grc, coded and uncoded,
                                      # unicast/broadcast, 3 stations at once, a 6 dB channel
python3 -m tests.test_grc_goodput 100 0.5   # needs GNU Radio: 100 KB through the flowgraph + MAC, FEC on vs off
python3 -m tests.test_fake_antsdr  # needs GNU Radio: TD-CDMA edge cases, every station the real app on cdma_phy.grc
                                   # with emulated ANTSDRs (section 5.4); about 30 min. --speed 0.4 for a slow PC
                                   # (default 0.6x real time), or pick some: test_fake_antsdr 3 8. Table below
python3 -m tests.test_ldpc         # LDPC encoder (all 51 lifting sizes), rate matching, AWGN curves, Golay
python3 -m tests.test_grc_cores    # the flowgraph's Python cores with the stock blocks imitated in numpy,
                                   # plus numpy-PHY <-> GRC-PHY interoperability (no GNU Radio needed)
python3 -m tests.test_modem        # PHY: impairments, false alarms, multi-user, collisions, SNR sweeps, CPU
python3 -m tests.test_profiles fast   # every modulation/code rate: SNR sweep, 2nd link, decode cost, real time
python3 -m tests.test_bigfile 1 fast 14 auto auto   # 1 MB at 14 dB with adaptive LDPC (last arg off = uncoded)
python3 -m tests.test_link         # MAC over simulated air (spread ALOHA): concurrency, priority, lossy link
python3 -m tests.test_tdma_phy     # the time slots' timing base: bursts on exact samples (numpy PHY, and the
                                   # flowgraph's blocks with stand-ins for GNU Radio), receive sample indices,
                                   # the TX -> RX offset under clock drift, TX underruns and lost RX buffers
python3 -m tests.test_edge         # TD-CDMA edge cases on the simulated air (30-50 min; --quick is shorter;
                                   # or pick some: test_edge 3 9 12). Table below
python3 -m tests.test_uno          # UNO rules engine: every rule, a player leaving, 300 random games (always 108 cards)
python3 -m tests.test_games 3      # game center over the simulated air, 2-4 stations: a UNO round, a player leaving,
                                   # a race (--transfer 400: a file transfer during UNO, --key: encrypted,
                                   # --mac aloha: the earlier link layer)
python3 -m tests.test_features     # profile pictures over the air, cancelling a transfer on either end, clock offsets,
                                   # names and pictures passed on by a relay, long names in beacons
python3 -m tests.test_power        # transmit power control: the link layer's loop, the flowgraph blocks carrying each
                                   # burst's power (stand-ins for GNU Radio), and near-far on the simulated air with
                                   # and without it, spread ALOHA (a few minutes; --quick skips the simulation)
python3 -m wavelink sim --nodes 3 & python3 -m tests.ui_games 3   # optional, needs Playwright: UNO, a race and a
                                   # rematch (chase camera), played with the keyboard on every station, a chat
                                   # message sent from the race and the radio dashboard opened over it;
                                   # screenshots in build/ui-shots/
python3 -m wavelink sim --nodes 2 & python3 -m tests.ui_features   # optional, needs Playwright and Pillow: themes,
                                   # the wallpaper editor (crop, turn, adjust, four fits, editing again from the
                                   # original), pictures, emoji, photo/voice quality (fake microphone), cancel, a
                                   # late message, new-message arrow, sounds, buttons at the bottom, camera (fake
                                   # camera), background music, chatting during UNO, the + tray (voice, camera,
                                   # file, emoji, priority over the game), the radio dashboard in the game center:
                                   # 65 checks
python3 -m tests.test_gr_backend   # numpy-PHY GNU Radio backend against a fake GR (API-shape test)
python3 -m tests.test_tx_underrun  # mid-burst TX underruns on a fake ANTSDR with a busy interpreter, per TX buffer size
python3 -m wavelink sim --nodes 3 & python3 -m tests.scenario_api   # full app scenario through the web API
                                   # (it waits until the stations have heard each other)
```

**TD-CDMA edge cases** (`tests/test_edge.py`, numpy simulator; every receiver's sample counter starts at a random
value and every link has a random delay of up to 100 µs, so nothing works unless the offsets are measured). The
simulated air also records every moment two stations transmit at once while a third hears both; once a network
has formed that must never happen, and the tests marked † check it (none seen):

| #       | Scenario                                                                                         | Result                                                                                                                                                                                                                                                                                               |
| ------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1       | Start orders: 3, then 1, then 2; four at once; two lone stations coming into range               | one network every time: the first on the air leads (four started together: station 1, after 1.4–2.3 s); two lone networks merge under the lower address                                                                                                                                             |
| 2 †    | A station joins and the lead leaves during a 300 KB transfer                                     | delivered intact (136–163 kb/s), no burst late                                                                                                                                                                                                                                                      |
| 3 †    | The lead's app closed mid-transfer, then started again                                           | its successor took over after 0.3–1.0 s (and only it), the transfer went on (167–181 kb/s; in a few runs 79–91 kb/s), the old lead came back as a member                                                                                                                                          |
| 4       | Two newcomers at the same moment; two stations with the same address                             | both had a slot after 0.2–1.2 s; the duplicate was reported on the dashboard                                                                                                                                                                                                                        |
| 5 †    | Clocks at +25, −25 and +10 ppm for 10 minutes, a 2.5 KB message every 2 s                       | all 299 messages delivered; D did not move (0 samples); each follower measured its clock against the lead's within 0.1 ppm (−49.9 / −50.1 and −14.9 / −15.1 ppm; true: −50, −15); no burst late, no slot missed (three runs)                                                                   |
| 6       | Six TX underruns and lost RX buffers during two transfers; a station that can't hear itself      | both delivered (39–78 kb/s), every jump of D measured again; the deaf station kept its timing from the others' reports (left: 56 µs of simulated link delay) and delivered at 82–141 kb/s                                                                                                         |
| 7, 8 † | Four senders at once; near-far without power control; both against spread ALOHA                  | section 3.5                                                                                                                                                                                                                                                                                          |
| 9 †    | Relaying switched off and on again during a relayed transfer (1 and 3 out of each other's range) | one network throughout (3 keeps time by the plan 2 repeats, relaying on or off); the transfer waits while relaying is off and completes when it is back                                                                                                                                              |
| 10 †   | A group message with a member out of range, then that member gone                                | a relayed copy for the far member, ticks from both; the next group file reaches the others and the gone member is given up                                                                                                                                                                           |
| 11      | Race with four players: one away and back, one gone for good                                     | the away car stays on the others' screens while the race goes on; back within the grace period it races on; the one gone longer is dropped on every station                                                                                                                                          |
| 12      | UNO: a player away and back; the host away; a player back too late                               | UNO pauses for everybody while a player is away and goes on when she is back, hand kept; with the host away the next player hosts, the old host's hand kept, and the old host, back, plays on under the new one; a player away too long is dropped, the pause ends, and she is told when she is back |
| 13 †   | Es/N0 5 dB; bursty interference 3 dB below the signal 25 % of the time                           | the network forms and delivers at 5 dB (2–14 retransmissions), and although lead headers get lost nobody takes the lead over; under interference 117–138 kb/s, 0–1 retransmissions                                                                                                                |
| 14 †   | A station misses every header that announces a plan change (16 changes)                          | it never sends by a stale plan: it holds its slot until it hears the plan in force (4–7 slots over 16–17 changes with the earlier 0.5 s window, none over 5 changes with 1 s), never overlaps anyone; everything delivered                                                                         |
| 15 †   | A station stops hearing the lead for 4 s (the others still do), relaying on and off              | no takeover: it marks its header *lead unheard*, keeps time by the plan another member repeats, and its transfer goes on (163–165 kb/s)                                                                                                                                                           |
| 16 †   | A station off the air for 1 s, then for 2.5 s                                                    | 1 s: keeps its slot; 2.5 s: its slot is freed and it has a new one 0.1–0.9 s after it is back                                                                                                                                                                                                       |
| 17 †   | A hidden station with relaying off everywhere                                                    | one network (timing repeated, no messages passed on); both outer stations reach the middle one                                                                                                                                                                                                       |

**TD-CDMA on GNU Radio with fake ANTSDRs** (`tests/test_fake_antsdr.py`). Every station is its own
`python3 -m wavelink run --uri fake:<air>/<slot>` process on the unchanged `grc/cdma_phy.grc`; only the two ANTSDR
blocks are emulated (`wavelink/fake_antsdr.py`: libiio's buffers, underruns, lost RX buffers, clipping), and the air
between them runs in the test (gains, delays, LO offsets, the own transmitter 25 dB up, AWGN, clock error, cut
links). It logs any two stations on the air at once where a third hears both, which must never happen; the tests
marked † check it (none seen). Results with GNU Radio 3.10.9.2 on a 2-core VM, the air at 0.4× real time: 55 of 58
checks.

| #     | Scenario                                                                                                   | Result                                                                                                                                                                    |
| ----- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | Start orders; three processes at once; three radios switched on at the same moment; two lone stations meet | one network every time, the first on the air leading; radios switched on together: led by station 1 after 4.8 s; two lone networks merged in 1.5 s                        |
| 2 †  | Three 60 KB files at once on three links, then a group message                                             | all intact (92, 72 and 51 kb/s), the group message reached both, no burst late                                                                                            |
| 3 †  | The lead's process killed mid-transfer, then started again                                                 | station 2 took over after 0.71 s, and only it; the transfer went on (150 kb/s, intact); the old lead came back as a member and could send                                 |
| 4     | Two radios set to address 2                                                                                | the warning "Another station is using address 2" appeared within seconds                                                                                                  |
| 5 †  | Clocks at +25, −25 and +10 ppm for 4 minutes of air                                                       | 80 of 80 messages; the followers measured −50.0 and −15.0 ppm; each D moved by exactly what its streams slipped                                                         |
| 6     | Five injected TX gaps and lost RX buffers during two transfers                                             | both intact (52 and 22 kb/s); every jump of D measured again, to the sample                                                                                               |
| 7 †  | A hidden station: 1 and 3 can't hear each other, 2 relays                                                  | one network; 40 KB from 1 to 3 through 2 at 43 kb/s, intact, and a message back                                                                                           |
| 8     | A station off the air for 1 s, then 3.5 s                                                                  | 1 s: keeps its slot; 3.5 s: dropped from the plan after 3.0 s, back in it 1.4 s after it returns                                                                          |
| 9     | Es/N0 10 dB; interference bursts 20 % of the time, 3 dB below a link                                       | one network, a file at 53 kb/s, no false takeover; under interference 111 kb/s                                                                                            |
| 10 † | Real time: the air at full speed, two stations                                                             | **fails on the 2-core VM** (CPU): 358 TX underruns and 124 lost RX buffers, station 2 never got into the plan and a file failed; still no burst late and no overlap |
| 11    | Spread ALOHA (`--mac aloha`) on the same flowgraph                                                       | a 40 KB file at 24 kb/s and a message                                                                                                                                     |
| 12    | Race and UNO over the slots                                                                                | each car's state reached both others in 60–98 % of cycles; 16 of 16 UNO moves seen by both others (median 0.80 s)                                                        |

Nine bugs showed up only here, none in the numpy simulator; the fixes are in the code (a hidden station's join, a
newcomer's first join request, bursts after a TX underrun, slots held while a slot grew, slots growing one station
at a time, a slow PC that stayed slow, a restart taken for a duplicate address, grcc's interpreter, two stations
compiling at once).

`tests.scenario_api` also runs against `python3 -m wavelink grc-sim` (same ports). `test_grc_flowgraph` and
`test_grc_goodput` pass with GNU Radio 3.10.9.2 (earlier with 3.10.12), but they run three station PHYs in one Python
process at 1 MS/s, which a 2-core PC can't always keep up with: there the 6 dB step of `test_grc_flowgraph` loses a
frame in some runs (a despreader falls 3 s behind and drops samples) and `test_grc_goodput`'s rates drop by half.
On a busy machine the two-radio part of `test_gr_backend` can also take longer than its 20 s; on a quiet one it
passes in 2–3 s. The game center tests
ran on the numpy simulator and, in test 12 above, over the fake radios. The ANTSDR blocks (`iio_pluto_sink/source`) have not been run
on a physical E200 yet, so expect to tune gains in the first lab session.

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
  samples and it dropped some (and while the queue fills, everything heard is up to 3 s late, so ACKs time out).
  Receiving costs more CPU the more of the time frames for this station are on the air (`tests.test_profiles`:
  ~2.9x real time idle, 1.2–1.7x with continuous traffic), so slow modulations and retransmissions, which put the
  same data on the air for longer, push a busy PC over the edge. Close other programs, and check the modulation per
  link on the dashboard: if the overflows only come with power control on, the links are running slower than they
  should. As a last resort lower `samp_rate` to 2e6 in the .grc on all stations (half the bit rate and half the
  CPU).
- **"Buffer too small for min_noutput_items" from a PDU to Tagged Stream block.** A burst is larger than the
  block's buffer. The shipped graph sets *Min Output Buffer* 32768 on the code lanes and the receive branches (a 1 KB
  frame at BPSK ½ is 16.5k symbols); keep it if you edit them, and raise it if you raise `max_payload`.
- **Retransmissions while two stations talk to each other at once (spread ALOHA).** Your own transmitter deafens
  your receiver. Separate the TX/RX antennas, lower TX power, or raise `--hd-defer`; or use TD-CDMA, the default.
- **TD-CDMA: "late bursts" climbing** (dashboard, *Time slots* panel). Bursts reached the radio after their slot had
  begun and were dropped (they would have landed in another station's slot). The flowgraph hands bursts over
  earlier by itself (up to 450 ms ahead); if the count keeps climbing the PC is overloaded: close other programs.
- **TD-CDMA: "D jumps", or "receiver search: everywhere (checking timing)".** A TX underrun or a lost RX buffer
  shifted the station's TX → RX offset; it measures it again within a few slots. If it happens often, raise
  `--tx-buffer` and see the RX overflow item above.
- **TD-CDMA: a station can't hear itself.** "our header as heard" on its dashboard then shows the others' timing
  reports, which steer it (section 3.1). It works, but more slowly; put its TX and RX antennas closer together.
- **TD-CDMA: two leads** (two dashboards both say *lead station*, and after a few seconds *Another network (lead:
  station N) is on the air here*). The two groups don't hear each other's headers, and their slots overlap. A
  station that hears both joins them into one; if none does, move the stations closer or lower `--tx-atten`.
- **"Another station is using address N".** Two stations run with the same `--id`. Give every station its own.
- **The microphone doesn't work.** Use `http://localhost`; browsers block the microphone on plain-HTTP remote
  addresses. You can attach an audio file instead.

## 9. Limitations and improvements (for the report)

- **TD-CDMA on GNU Radio: so far with fake radios.** The real flowgraph ran on GNU Radio 3.10.9 with emulated
  ANTSDRs, one process per station (`tests/test_fake_antsdr.py`, section 7), with the air at 0.4× real time because
  the test PC has 2 cores. Not modelled: the analog front end (AGC, IQ imbalance, DC offset, phase noise,
  multipath), the E200's Ethernet link and the real levels. The stand-in radio blocks are Python while gr-iio's are
  C++, so they lose RX buffers more easily than the real ones. On the lab PCs watch the console for "TX->RX offset jumped" and "radio
  stream gap" and the dashboard's late bursts; `--mac aloha` is the fallback.
- **CPU.** A station needs about 1.1 cores of a 2.8 GHz Xeon per second of air when idle, 1.2 when sending and 2.1
  when receiving at 153 kb/s (GNU Radio with fake ANTSDRs). 1.6 of the 2.1 are Python under one interpreter lock:
  the despreader's search and tracking thread, the link layer, and the combiner, despreader and LDPC blocks. Two
  stations in real time overloaded the 2-core test PC (lost RX buffers, a failed transfer: section 7, test 10). A
  lab PC should have 4 or more cores and little else running, and real time on one is still to be shown; porting
  the despreader's search and the combiner to C++ would remove most of the load.
- **TD-CDMA against spread ALOHA.** Where a station could receive while it transmits, ALOHA carries more in total
  (294–321 against 160–180 kb/s for 4 senders in the idealised simulator); on radios that deafen themselves TD-CDMA
  carries three times as much (173–181 against 53–58 kb/s, section 3.5). A single link gets ~80 % of ALOHA's speed:
  each cycle also has the receiver's slot, the join slot (11.5 ms) and the headers (5–15 ms each).
- **Inside a slot the codes are Gold codes, not orthogonal Walsh codes.** Walsh codes would remove the interference
  between a slot's codes (29.4 dB with 2 codes, 25.3 dB with 3), but they need chip-synchronous bursts under one
  scrambling code, a spreading factor of 32 and new preambles on both PHYs. With Gold codes that floor is already
  above what uncoded 16-QAM needs (22.5 dB), so it was left out.
- **Relaying is one hop**, through a station that hears both ends; there is no routing over several relays.
  Beacons are not passed on as such; instead a relaying station lists, in its own beacon, the names and picture
  tags of the stations a neighbour can't hear, so a station reached only through a relay still shows its name
  and picture.
- **Merging networks.** Within a formed network slots never overlap (checked throughout `tests/test_edge.py`). When
  two networks meet (a station back after more than 3 s alone, or two groups coming into range), the slots of the
  one that yields can overlap the other's for a cycle or two, until its stations have joined.
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
- **Throughput ceiling.** About 236 kb/s goodput per link at 4 MS/s in the numpy simulator with spread ALOHA
  (uncoded 16-QAM on a clean link), 190 kb/s with TD-CDMA (the other slots of the cycle), limited by Python DSP CPU
  (not yet measured on hardware with the GRC PHY). A C++ OOT correlator and
  LDPC decoder would allow higher chip rates; 64-QAM ⅚ would need ~19 dB.
- **Power control.** Digital only: below the target a burst simply uses fewer DAC steps, and the transmitter's own
  noise floor and LO leakage do not drop with it. In a small lab with stations a metre apart, also raise
  `--tx-atten` so the backoff stays well inside its 30 dB. Two stations sending to the *same* receiver share its
  code, which power control can't separate (ARQ resolves those collisions). Taking only the power a link can't use
  (target 25.5 dB) protects weak links less than a lower target would; the near-far table in 3.4 shows the cost.
  The loop has only been tested in the simulators: on the ANTSDRs, compare a transfer with the dashboard's switch
  on and off, and watch "our power", "power margin raises" and the modulation per link.
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
- **Game center.** When the host goes off the air the next player carries on from the last copy of the table it
  got, so a move the old host took but never sent out is lost. The copy of the UNO table in every view stays in the
  station, but a player who digs into their station's memory could read the other hands. With spread ALOHA, race datagrams share the broadcast code with beacons and
  occasionally collide with them, and on the GNU Radio PHY a game message can't overtake file frames already in its
  code lane (up to 0.6 s); with TD-CDMA neither happens. While a file transfer stretches the TD-CDMA cycle, every
  car is heard less often (4.7 updates a second at 213 ms, 2.4 at 415 ms, against 10 when idle). There is no
  spectator mode, and no computer players. Not yet tested on `grc-sim` or the ANTSDRs.
- **Message order.** The clock-offset estimate includes the shortest delivery time (tens of milliseconds, up to
  ~0.1 s with TD-CDMA) and follows a clock that is reset only after the old samples age out (5 minutes), so for a
  few minutes after a station's clock jumps its messages can be placed by up to the jump. Running NTP on the lab PCs
  avoids it.
- **Pictures and settings.** A profile photo is sent to a station that asks for it, but there is no way to push a
  new photo before its next beacon (up to 5 s). The wallpaper stays on its station. Voice-note re-encoding needs
  WebCodecs (Chrome, Edge, Firefox 130+); other browsers fall back to re-recording the clip in real time.

## 10. Suggested split for a group of four

| Member | Owns                                                                                                                                                 |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| A      | PHY DSP and coding: `codes.py`, `modem.py`, `ldpc.py`, `golay.py`, BER/FER measurements, hardware gain tuning                                |
| B      | GNU Radio and hardware: `grc/cdma_phy.grc`, `grc_phy.py`, ANTSDR firmware/IPs, `tx`/`rx` mid-eval demo                                       |
| C      | Link layer: `mac.py`, `tdma.py` (time slots, relaying), ARQ, adaptive modulation-and-coding table, power control, priority tests, the simulators |
| D      | Application and UI: `node.py`, `web/`, `games/` (game center), encryption, dashboard, demo script                                              |

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
  mac.py         link layer (ARQ, adaptive modulation-and-coding table, power control; spread ALOHA)
  tdma.py        TD-CDMA time slots: lead, slot plan, joining, takeover, TX -> RX offset, link tables, relaying
  fake_antsdr.py stand-ins for the two ANTSDR blocks and the air between them (run --uri fake:, fake-air)
  crypto.py      AES-256-GCM
  node.py        application layer
  games/         game center: hub.py (rooms, invitations, clock sync, UNO host, race slots), uno.py (rules
                 engine), race.py (race datagram format)
  web/           server.py + static/: index.html, app.js, style.css (chat); settings.js (profile, themes,
                 wallpapers), walledit.js (wallpaper editor), media.js (quality sheets, recorder, camera, WebM
                 muxer), emojipick.js + emoji.js (palette), sfx.js (sound effects), music.js (background music),
                 gcchat.js (chat in the game center, its + tray), wavelink-*.png (logo, mark, icons), Montserrat (wordmark);
                 games.js, uno.js, race.js, games.css (game center); bundled fonts' licence FONTS-OFL.txt
  __main__.py    CLI: run | grc-sim | sim | fake-air | tx | rx | selftest
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
                 TD-CDMA timing base, TD-CDMA edge cases, fake ANTSDRs, power control, TX underruns,
                 GR backend, API scenario, UNO rules, game center (simulated air), game center in the browser,
                 station features, chat features in the browser
```
