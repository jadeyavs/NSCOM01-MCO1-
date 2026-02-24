# Reliable Data Transfer over UDP (RDT-UDP) — Mini RFC

---

## 1. Introduction

This document specifies **RDT-UDP** (Reliable Data Transfer over UDP), a lightweight application-layer protocol that provides reliable, ordered, and error-checked file transfer over an inherently unreliable UDP transport layer.

RDT-UDP emulates key behaviors of TCP — specifically connection establishment, sequenced delivery, acknowledgment-based flow control, and graceful termination — without requiring the full overhead of the TCP stack. The protocol is designed for point-to-point file transfer between a single client and a single server, and supports two discrete transfer operations: **UPLOAD** (client sends a file to the server) and **DOWNLOAD** (client retrieves a file from the server).

### Core Behavioral Constraints

- The protocol enforces **Stop-and-Wait ARQ**. At any given time, exactly one unacknowledged packet may be in-flight per session.
- Every transmitted packet MUST carry a **Sequence Number** and MUST receive an explicit **ACK** before the next packet is sent.
- All sessions are scoped by a randomly generated **Session ID** selected by the client at connection time.
- All multi-byte header fields MUST be transmitted in **Network Byte Order** (Big-Endian).
- The server sanitizes all client-supplied filenames using `os.path.basename()` and constrains all file I/O to the `server_data/` directory.

### Implementation Parameters

| Parameter            | Value          | Description                                               |
| :------------------- | :------------- | :-------------------------------------------------------- |
| Default Port         | `8080`         | UDP port the server binds to (`0.0.0.0:8080`)            |
| Max Payload Size     | `1024` bytes   | Maximum file data per single DATA packet                  |
| Header Size          | `12` bytes     | Fixed header size for all packet types                    |
| Timeout              | `2.0` seconds  | Retransmission timer interval                             |
| Stale Session TTL    | `10.0` seconds | `TIMEOUT × 5`; inactivity threshold for session cleanup   |
| Session ID Range     | `[1, 10000]`   | Random selection by the client at connection init         |
| Initial Seq Range    | `[1, 100]`     | Random selection by the client at connection init         |
| Server Storage Dir   | `server_data/` | Root directory for all server-side file reads and writes  |
| Max Datagram Size    | `1036` bytes   | `HEADER_SIZE (12) + MAX_PAYLOAD_SIZE (1024)`              |

---

## 2. Protocol Overview

Every RDT-UDP session proceeds through three sequential phases, regardless of whether the operation is an UPLOAD or DOWNLOAD:

**Phase 1 — Connection Establishment**
The client sends a SYN packet containing the requested operation and filename. The server validates the request, creates a session, and responds with a SYN-ACK. For DOWNLOAD sessions, the server immediately follows the SYN-ACK with the first DATA packet.

**Phase 2 — Data Transfer**
The data sender transmits sequential DATA packets. The receiver acknowledges each packet with an ACK matching the packet's sequence number. The sender does not advance to the next packet until the current one is acknowledged. Unacknowledged packets are retransmitted after a 2.0-second timeout.

**Phase 3 — Connection Termination**
When the data sender reaches EOF on the source file, it transmits a FIN packet. The receiver acknowledges it with an ACK, closes the file, and destroys the session. The FIN is subject to the same Stop-and-Wait and retransmission rules as DATA packets.

RDT-UDP establishes sessions via a handshake, transfers data strictly sequentially, and gracefully terminates via FIN exchanges.

### 2.1 Swimlane Diagrams and Protocol Message Exchange Descriptions

Each exchange below is represented as a time-ordered sequence of messages. Columns represent participants; rows represent progression through time. Direction arrows (`-->` and `<--`) indicate the sender and intended recipient.

---

#### Exchange 1: Successful File Download (Server → Client)

The client requests a file from the server. The server is the data sender; the client is the data receiver.

```
CLIENT                                     SERVER
  |                                           |
  |--- SYN ---------------------------------->|  Type=0, Seq=N, Session=S
  |    Payload: "DOWNLOAD|filename.txt"       |  [Server validates file exists]
  |                                           |  [Server opens file for reading]
  |                                           |  [Server creates session record]
  |<-- SYN-ACK -------------------------------|  Type=1, Seq=N+1, Session=S
  |    Payload: "OK"                          |  [Server immediately calls send_next_data()]
  |                                           |
  |<-- DATA ----------------------------------|  Type=2, Seq=N+2, Session=S
  |    Payload: <chunk 1, up to 1024 bytes>   |  [Server stores packet as unacked_packet]
  |    [Client validates seq == expected_seq] |
  |    [Client writes chunk to disk]          |
  |--- ACK ---------------------------------->|  Type=3, Seq=N+2, Session=S
  |                                           |  [Server clears unacked_packet]
  |                                           |  [Server calls send_next_data()]
  |<-- DATA ----------------------------------|  Type=2, Seq=N+3, Session=S
  |    Payload: <chunk 2, up to 1024 bytes>   |
  |--- ACK ---------------------------------->|  Type=3, Seq=N+3, Session=S
  |                                           |
  |          ... repeats per chunk ...        |
  |                                           |
  |<-- FIN -----------------------------------|  Type=4, Seq=M, Session=S
  |    [Server transitions to FIN_WAIT]       |  [EOF reached; FIN stored as unacked_packet]
  |    [Client flushes and closes file]       |
  |--- ACK ---------------------------------->|  Type=3, Seq=M, Session=S
  |                                           |  [Server receives ACK in FIN_WAIT]
  |                                           |  [Server closes file; destroys session]
  |          [DONE]                           |          [CLOSED]
```

**Key sequencing detail:** The client sets `expected_seq = client_initial_seq + 2` immediately after receiving the SYN-ACK. This accounts for the SYN consuming offset 0 and the SYN-ACK consuming offset 1, making the first server DATA always `Seq = N+2`.

---

#### Exchange 2: Successful File Upload (Client → Server)

The client sends a file to the server. The client is the data sender; the server is the data receiver.

```
CLIENT                                     SERVER
  |                                           |
  |--- SYN ---------------------------------->|  Type=0, Seq=N, Session=S
  |    Payload: "UPLOAD|filename.txt"         |  [Server opens file for writing]
  |                                           |  [Server creates session; expected_seq = N+1]
  |<-- SYN-ACK -------------------------------|  Type=1, Seq=N+1, Session=S
  |    Payload: "OK"                          |
  |    [Client increments seq_num by 1]       |
  |                                           |
  |--- DATA --------------------------------->|  Type=2, Seq=N+1, Session=S
  |    Payload: <chunk 1, up to 1024 bytes>   |  [Server: seq == expected_seq]
  |                                           |  [Server writes chunk to disk]
  |                                           |  [Server increments expected_seq to N+2]
  |<-- ACK -----------------------------------|  Type=3, Seq=N+1, Session=S
  |    [Client increments seq_num to N+2]     |
  |                                           |
  |--- DATA --------------------------------->|  Type=2, Seq=N+2, Session=S
  |    Payload: <chunk 2, up to 1024 bytes>   |
  |<-- ACK -----------------------------------|  Type=3, Seq=N+2, Session=S
  |                                           |
  |          ... repeats per chunk ...        |
  |                                           |
  |--- FIN ---------------------------------->|  Type=4, Seq=M, Session=S
  |    [Client enters retransmit loop]        |  [Server: handle_fin() invoked]
  |                                           |  [Server closes and flushes file]
  |                                           |  [Server destroys session]
  |<-- ACK -----------------------------------|  Type=3, Seq=M, Session=S
  |    [Client exits send_fin() loop]         |
  |          [DONE]                           |          [CLOSED]
```

---

#### Exchange 3: Retransmission — Lost DATA Packet (Download Scenario)

A DATA packet from the server is lost in transit. The server's retransmission timer fires and the packet is re-sent.

```
CLIENT                                     SERVER
  |                                           |
  |          <-- DATA (Seq: N) ----------X    |  [Packet lost; client receives nothing]
  |                                           |
  |     ... 2.0s elapses ...                  |
  |                                           |  [check_timeouts() fires]
  |                                           |  [time.time() - last_send_time > TIMEOUT]
  |<--- DATA (Seq: N) [RETRANSMIT] -----------|  [Same packet re-sent from unacked_packet]
  |    [Client validates seq == expected_seq] |
  |    [Client writes chunk to disk]          |
  |--- ACK (Seq: N) ------------------------->|  [Server clears unacked_packet]
  |                                           |  [Normal flow resumes; next chunk sent]
  |<-- DATA (Seq: N+1) -----------------------|
```

---

#### Exchange 4: Retransmission — Lost ACK Packet (Upload Scenario)

The server successfully writes a DATA chunk but its ACK is lost. The client times out and retransmits the DATA packet. The server handles the duplicate without re-writing data.

```
CLIENT                                      SERVER
  |                                            |
  |--- DATA (Seq: N) ------------------------->|  [Server: seq == expected_seq]
  |                                            |  [Server writes chunk to disk]
  |                                            |  [Server increments expected_seq to N+1]
  |     ACK (Seq: N) ----------X               |  [ACK lost in transit]
  |                                            |
  |    ... 2.0s elapses ...                    |
  |    [socket.timeout in upload inner loop]   |
  |--- DATA (Seq: N) [RETRANSMIT] ------------>|  [Server: seq < expected_seq -> DUPLICATE]
  |                                            |  [Server does NOT write to disk again]
  |<-- ACK (Seq: N) ---------------------------|  [Server re-sends ACK to unblock client]
  |    [Client: seq == current_seq -> advance] |
  |--- DATA (Seq: N+1) ----------------------->|  [Normal flow resumes]
```

---

#### Exchange 5: Connection Error — File Not Found (Download)

```
CLIENT                                    SERVER
  |                                          |
  |--- SYN --------------------------------->|  Type=0, Payload: "DOWNLOAD|missing.txt"
  |                                          |  [os.path.exists(filepath) -> False]
  |                                          |  [No session created]
  |<-- ERROR --------------------------------|  Type=6, Seq=N+1, Session=S
  |    Payload: "File not found"             |
  |    [Client logs error; aborts]           |
  |          [DONE -- no file written]       |
```

---

#### Exchange 6: Connection Error — Invalid SYN Payload

```
CLIENT                                    SERVER
  |                                          |
  |--- SYN --------------------------------->|  Type=0, Payload: "malformed_string"
  |                                          |  [payload.split('|', 1) raises ValueError]
  |                                          |  [No session created]
  |<-- ERROR --------------------------------|  Type=6, Seq=N+1, Session=S
  |    Payload: "Invalid SYN payload format" |
  |    [Client logs error; aborts]           |
```

---

## 3. Packet Message Formats

All RDT-UDP packets share a single fixed-length 12-byte binary header, immediately followed by a variable-length payload. The entire structure is transmitted as a single UDP datagram.

### 3.1 Header Layout

The header consists of five fields packed contiguously in **Network Byte Order** (Big-Endian), using the Python `struct` format string `!B I I H B`.

| Offset (bytes) | Field               | Size    | Type     | Description                                                                                       |
| :------------- | :------------------ | :------ | :------- | :------------------------------------------------------------------------------------------------ |
| `0`            | **Type**            | 1 byte  | `uint8`  | Message type identifier. See Section 3.2 for all valid values.                                    |
| `1`            | **Sequence Number** | 4 bytes | `uint32` | Monotonically increasing counter. Scoped per-session. Increments by 1 for each new transmitted packet. |
| `5`            | **Session ID**      | 4 bytes | `uint32` | Randomly assigned identifier used to isolate one transfer session from another.                   |
| `9`            | **Payload Length**  | 2 bytes | `uint16` | Byte count of the payload data that follows the header. `0` for control packets with no payload.  |
| `11`           | **Checksum**        | 1 byte  | `uint8`  | XOR checksum computed over all preceding header bytes and all payload bytes.                      |

**Serialization format breakdown (`struct` format string `!B I I H B`):**

- `!` — Forces Big-Endian (Network) byte order for all fields
- `B` — Type field: 1 byte unsigned char
- `I` — Sequence Number: 4 bytes unsigned int
- `I` — Session ID: 4 bytes unsigned int
- `H` — Payload Length: 2 bytes unsigned short
- `B` — Checksum: 1 byte unsigned char

**Maximum total packet size:** `12 (header) + 1024 (payload) = 1036 bytes`

*Note: The payload is truncated to `payload_length` bytes upon deserialization in `Packet.from_bytes()` as a safety measure against oversized datagrams.*

---

### 3.2 Message Types (`Type` Field)

*   `0 (SYN)`: Connection initialization request. Payload contains the operation and filename (e.g., `DOWNLOAD|file.txt`).
*   `1 (SYN-ACK)`: Acknowledgment of initialization and parameter agreement.
*   `2 (DATA)`: Carries the actual file data chunk in the payload.
*   `3 (ACK)`: Acknowledges successful receipt of a packet.
*   `4 (FIN)`: Indicates no more data to send; initiates connection termination.
*   `5 (FIN-ACK)`: Acknowledges the termination request.
*   `6 (ERROR)`: Indicates an error occurred (e.g., File Not Found). Payload contains the error description string.

## 4. State Machines

### 4.1 Client State Machine

The client is ephemeral; it manages one connection at a time per invocation. State is managed implicitly through the control flow of `connect()`, `download_file()`, `upload_file()`, and `send_fin()`.

#### Client States

| State            | Description                                                                                                    |
| :--------------- | :------------------------------------------------------------------------------------------------------------- |
| **CLOSED**       | Initial state before any operation begins, and the terminal state after a session ends or aborts.              |
| **SYN_SENT**     | A SYN has been transmitted. Waiting for SYN-ACK or ERROR. Timer is active; SYN is retransmitted on timeout.   |
| **TRANSFERRING** | SYN-ACK received and validated. Actively exchanging DATA and ACK packets with the server.                      |
| **FIN_SENT**     | Upload only. All DATA chunks sent; FIN has been transmitted. Waiting for server's ACK. Timer is active.        |
| **DONE**         | Terminal state. Session was completed successfully or aborted due to an error.                                  |

#### Client State Transition Table

| Current State              | Event / Trigger                                   | Action Taken                                                  | Next State              |
| :------------------------- | :------------------------------------------------ | :------------------------------------------------------------ | :---------------------- |
| CLOSED                     | `download_file()` or `upload_file()` invoked      | Generate random `seq_num` and `session_id`; send SYN          | SYN_SENT                |
| SYN_SENT                   | SYN-ACK received; `session_id` matches            | Record acknowledged seq; begin data phase                     | TRANSFERRING            |
| SYN_SENT                   | ERROR received; `session_id` matches              | Log server error message; abort operation                     | DONE                    |
| SYN_SENT                   | 2.0s socket timeout                               | Retransmit SYN packet (identical)                             | SYN_SENT                |
| TRANSFERRING (DOWNLOAD)    | DATA received; `seq == expected_seq`              | Write payload to file; send ACK; increment `expected_seq`     | TRANSFERRING            |
| TRANSFERRING (DOWNLOAD)    | DATA received; `seq < expected_seq`               | Re-send ACK for duplicate; do not write data                  | TRANSFERRING            |
| TRANSFERRING (DOWNLOAD)    | FIN received                                      | Send ACK; flush and close output file                         | DONE                    |
| TRANSFERRING (DOWNLOAD)    | ERROR received                                    | Log error; abort without closing file cleanly                 | DONE                    |
| TRANSFERRING (DOWNLOAD)    | 2.0s socket timeout                               | Log waiting message; continue (server will retransmit)        | TRANSFERRING            |
| TRANSFERRING (UPLOAD)      | ACK received; `seq == current seq_num`            | Increment `seq_num`; read next chunk                          | TRANSFERRING or FIN_SENT|
| TRANSFERRING (UPLOAD)      | 2.0s socket timeout (waiting for ACK)             | Retransmit current DATA packet                                | TRANSFERRING            |
| TRANSFERRING (UPLOAD)      | `f.read()` returns empty bytes (EOF)              | Call `send_fin()`; send FIN packet                            | FIN_SENT                |
| FIN_SENT                   | ACK received; `seq == fin seq`; session matches   | Log upload complete                                           | DONE                    |
| FIN_SENT                   | 2.0s socket timeout                               | Retransmit FIN packet (identical)                             | FIN_SENT                |

---

### 4.2 Server State Machine

The server is persistent and handles multiple concurrent sessions. Each session is an entry in `self.sessions`, keyed by `session_id`. The server-level `start()` loop and `check_timeouts()` routine advance each session's state independently. The server itself perpetually remains in a LISTEN-equivalent top-level state; per-session state is tracked within each dictionary entry.

#### Server Session States

| State            | Description                                                                                                                      |
| :--------------- | :------------------------------------------------------------------------------------------------------------------------------- |
| **LISTEN**       | Server-level perpetual state. Always accepting new SYN packets. No per-session state exists yet.                                 |
| **TRANSFERRING** | Active data exchange. For DOWNLOAD: server sending DATA, waiting for ACKs. For UPLOAD: server receiving DATA, sending ACKs.      |
| **FIN_WAIT**     | DOWNLOAD sessions only. Server has sent FIN and is waiting for the client's final ACK. FIN is held in `unacked_packet`.          |
| **CLOSED**       | Terminal state. Session record deleted from `self.sessions`. File object closed and dereferenced.                                 |

#### Server State Transition Table

| Current State              | Event / Trigger                                               | Action Taken                                                                         | Next State        |
| :------------------------- | :------------------------------------------------------------ | :----------------------------------------------------------------------------------- | :---------------- |
| LISTEN                     | SYN received; op=DOWNLOAD; file exists                        | Open file (`'rb'`); create session; send SYN-ACK; call `send_next_data()`            | TRANSFERRING      |
| LISTEN                     | SYN received; op=UPLOAD                                       | Open file (`'wb'`); create session; set `expected_seq=SYN.seq+1`; send SYN-ACK      | TRANSFERRING      |
| LISTEN                     | SYN received; op=DOWNLOAD; file does not exist                | Send ERROR (`"File not found"`); do not create session                               | LISTEN            |
| LISTEN                     | SYN received; payload missing `\|` separator                  | Send ERROR (`"Invalid SYN payload format"`); do not create session                   | LISTEN            |
| LISTEN                     | Non-SYN packet; `session_id` not in `self.sessions`           | Log unknown session; discard packet                                                  | LISTEN            |
| TRANSFERRING (DOWNLOAD)    | ACK received; `seq == unacked_packet.seq`                     | Clear `unacked_packet`; call `send_next_data()`                                      | TRANSFERRING      |
| TRANSFERRING (DOWNLOAD)    | `send_next_data()` reads empty bytes (EOF)                    | Send FIN; set `state='FIN_WAIT'`; store FIN as `unacked_packet`                      | FIN_WAIT          |
| TRANSFERRING (DOWNLOAD)    | Timeout; `unacked_packet` set; `elapsed > 2.0s`               | Retransmit `unacked_packet`; reset `last_send_time`                                  | TRANSFERRING      |
| TRANSFERRING (UPLOAD)      | DATA received; `seq == expected_seq`                          | Write payload to file; send ACK; increment `expected_seq`                            | TRANSFERRING      |
| TRANSFERRING (UPLOAD)      | DATA received; `seq < expected_seq` (duplicate)               | Re-send ACK for duplicate sequence; do not write data                                | TRANSFERRING      |
| TRANSFERRING (UPLOAD)      | DATA received; `seq > expected_seq` (out-of-order)            | Log out-of-order; discard packet                                                     | TRANSFERRING      |
| TRANSFERRING (UPLOAD)      | FIN received                                                  | Send ACK; close file; `del self.sessions[session_id]`                                | CLOSED            |
| FIN_WAIT                   | ACK received; `seq == fin_pkt.seq`                            | Close file; `del self.sessions[session_id]`                                          | CLOSED            |
| FIN_WAIT                   | Timeout; `unacked_packet` set; `elapsed > 2.0s`               | Retransmit FIN packet; reset `last_send_time`                                        | FIN_WAIT          |
| Any                        | `time.time() - last_send_time > TIMEOUT × 5` (10.0s idle)    | Close file; `del self.sessions[session_id]` (stale cleanup)                          | CLOSED            |

---

## 5. Reliability Mechanisms

### 5.1 Stop-and-Wait ARQ

RDT-UDP enforces the Stop-and-Wait (SAW) Automatic Repeat reQuest discipline throughout all data transfer phases. The protocol invariant is: **at any instant, exactly one unacknowledged packet is permitted per session.**

The mechanics differ between client and server due to their respective architectures:

**Client-side SAW (upload data sender):**
Within `upload_file()`, an inner `while True` loop retains control after each `sock.sendto()` call. The loop blocks on `sock.recvfrom()` with a 2.0-second timeout. If the timeout fires, the identical packet is retransmitted. If a matching ACK arrives, the loop exits, `seq_num` is incremented, and the next chunk is read. The same loop structure is used in `send_fin()` for FIN retransmission.

**Server-side SAW (download data sender):**
The server is event-driven via UDP `recvfrom()`. The server stores the last-sent packet as `session['unacked_packet']`. `send_next_data()` is a no-op if `unacked_packet is not None`, preventing any new packet from being sent while one is outstanding. The `check_timeouts()` method, called whenever the server socket times out (approximately every 2.0 seconds of inactivity), inspects each session and retransmits `unacked_packet` if `time.time() - last_send_time > TIMEOUT`.

**Client-side receive (download data receiver):**
The client does not enforce Stop-and-Wait in the receive path, since it is not the sender. It validates incoming packet sequence numbers and issues ACKs, relying on the server's SAW discipline to ensure only one packet arrives at a time.

---

### 5.2 Sequence Numbers

Every packet carries a 32-bit unsigned Sequence Number. The client selects a random initial value in the range `[1, 100]`. The sequence number increments by 1 for each new packet dispatched within a session.

The sequencing contract per session phase is as follows:

| Event                                      | Sequence Number                                               |
| :----------------------------------------- | :------------------------------------------------------------ |
| Client SYN                                 | `N` (initial random in `[1, 100]`)                            |
| Server SYN-ACK                             | `N + 1`                                                       |
| Server first DATA (DOWNLOAD)               | `N + 2` (server increments `seq_num` before each DATA send)   |
| Client first DATA (UPLOAD)                 | `N + 1` (client increments `seq_num` by 1 after SYN-ACK)     |
| Each subsequent DATA packet                | Previous DATA `Seq + 1`                                       |
| FIN (server, DOWNLOAD)                     | Last DATA `Seq + 1` (via `seq_num += 1` inside `send_next_data()`) |
| FIN (client, UPLOAD)                       | `self.seq_num` at time of EOF (same as last ACKed DATA seq)   |

**Receiver-side sequence validation — three outcomes:**

- `packet.seq_num == expected_seq` — Accepted. Payload written; ACK sent; `expected_seq` incremented.
- `packet.seq_num < expected_seq` — Duplicate detected. ACK re-sent for the duplicate; payload discarded. No state change.
- `packet.seq_num > expected_seq` — Out-of-order packet. Logged and silently discarded. No ACK sent; no state change. (Selective ACK is not supported.)

---

### 5.3 Checksum (XOR Integrity Check)

A 1-byte XOR checksum is computed over the complete packet — both the header (excluding the checksum byte itself) and the payload — using the following algorithm executed in `Packet._calculate_checksum()`:

```
header_bytes = struct.pack('!B I I H', type, seq_num, session_id, payload_length)

checksum = 0
for b in header_bytes:
    checksum ^= b
for b in payload:
    checksum ^= b
```

The resulting checksum is placed in byte offset `11` of the 12-byte header and transmitted with the packet.

On reception, `Packet.from_bytes()` unpacks the header, extracts the received checksum, reconstructs the `Packet` object (which recomputes the checksum in `__init__`), and compares the two values. If they differ, a `ValueError` is raised and the packet is silently discarded. No NACK is generated; the sender's timeout timer will eventually trigger retransmission.

**Known limitations of the 1-byte XOR checksum:**
- A single-bit flip in any field will be detected.
- Two identical bit-position errors in separate bytes cancel each other out and will **not** be detected.
- The 1-byte width provides only 256 distinct checksum values, allowing some corrupted packets to pass by coincidence.
- Payload byte reordering within a packet may go undetected depending on the reordering pattern.
- For production use, a CRC-32 or stronger checksum is recommended.

---

### 5.4 Duplicate Packet Handling

Both sides implement idempotent duplicate rejection to handle the scenario where an ACK is lost and the data packet is retransmitted.

**Server (UPLOAD receiver):** In `handle_data()`, if `packet.seq_num < session['expected_seq']`, the packet is a retransmission of an already-written chunk. The server does **not** write the payload to the file again. It re-sends `ACK(seq=packet.seq_num)` to unblock the client.

**Client (DOWNLOAD receiver):** In the `download_file()` receive loop, if `packet.seq_num < expected_seq`, the packet is a duplicate DATA frame. The client does **not** write the payload to the output file again. It re-sends `ACK(seq=packet.seq_num)` to unblock the server.

This ensures **exactly-once write semantics** for all file data regardless of network-induced retransmissions, as long as sequence numbers remain monotonically ordered within the session.

---

### 5.5 Retransmission Timer Summary

| Sender Component          | Timer Mechanism                          | Duration  | Trigger Condition                                        |
| :------------------------ | :--------------------------------------- | :-------- | :------------------------------------------------------- |
| Client — SYN              | `socket.settimeout(2.0)` on `recvfrom`   | 2.0s      | No SYN-ACK received within 2.0s                          |
| Client — DATA (upload)    | `socket.settimeout(2.0)` on `recvfrom`   | 2.0s      | No ACK received within 2.0s in upload inner loop         |
| Client — FIN              | `socket.settimeout(2.0)` on `recvfrom`   | 2.0s      | No ACK received within 2.0s in `send_fin()` loop         |
| Server — DATA (download)  | `check_timeouts()` via `socket.timeout`  | 2.0s poll | `unacked_packet` is set and `elapsed > TIMEOUT`          |
| Server — FIN (download)   | `check_timeouts()` via `socket.timeout`  | 2.0s poll | `unacked_packet` is set and `elapsed > TIMEOUT` in FIN_WAIT |

---

## 6. Error Handling

*   **File Not Found:** The Server MUST reply with an `ERROR` packet (Type 6) if the requested file doesn't exist.
*   **Session Mismatch:** Unrecognized `Session ID` packets MUST be ignored to prevent crossover.
*   **Unresponsive Peers:** The Server SHOULD implement a stale session cleanup (drop inactive sessions after `TIMEOUT * 5` seconds).

## 7. File Transfer Operations

*   **Download:** The Client sends a `SYN` packet with `DOWNLOAD|filename`. The Server locates the file in its operating directory and responds with `SYN-ACK`. The Server then reliably transmits `DATA` packets natively containing the binary file. The Client saves these to disk.
*   **Upload:** The Client sends `SYN` with `UPLOAD|filename`. The Server creates a blank file in its operating directory. The Client transmits `DATA` packets containing the file. The Server writes received packets to the disk.

## 8. End-of-file Signaling and Protocol Termination

When a sender reads EOF:
1.  It MUST send a `FIN` packet and start its timer.
2.  The receiver MUST flush data to disk and reply with an `ACK`.
3.  Upon receiving the `ACK`, the sender MUST close the session.
