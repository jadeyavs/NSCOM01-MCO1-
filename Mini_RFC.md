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

### 3.2 Message Types

#### Type 0 — SYN (Session Initiation Request)

Sent exclusively by the client to request a new session. Carries the operation type and target filename as a UTF-8 encoded, pipe-delimited payload string.

| Field          | Value                                                            |
| :------------- | :--------------------------------------------------------------- |
| Type           | `0`                                                              |
| Sequence Num   | Client's randomly chosen initial value, range `[1, 100]`         |
| Session ID     | Client's randomly chosen session identifier, range `[1, 10000]`  |
| Payload Length | Length of the payload string in bytes                            |
| Payload        | `"UPLOAD|<filename>"` or `"DOWNLOAD|<filename>"` (UTF-8)         |

The server splits the payload on the first `|` character. The left token must be exactly `"UPLOAD"` or `"DOWNLOAD"`. The right token is treated as the filename and is immediately sanitized with `os.path.basename()` before any filesystem access.

---

#### Type 1 — SYN-ACK (Session Acknowledgment)

Sent by the server in response to a valid SYN. Signals that the session has been accepted and the file operation is confirmed. No client acknowledgment of the SYN-ACK is required.

| Field          | Value                                                   |
| :------------- | :------------------------------------------------------ |
| Type           | `1`                                                     |
| Sequence Num   | Client's SYN `Seq + 1`                                  |
| Session ID     | Mirrored from the SYN packet                            |
| Payload Length | 2 (length of `"OK"`)                                    |
| Payload        | `"OK"` (UTF-8)                                          |

For DOWNLOAD sessions, the server calls `send_next_data()` immediately after dispatching the SYN-ACK, without waiting for any response from the client. For UPLOAD sessions, the server transitions to `TRANSFERRING` state and waits for incoming DATA packets.

---

#### Type 2 — DATA (File Data Chunk)

Carries a contiguous segment of the file being transferred. Sent by the server during DOWNLOAD and by the client during UPLOAD.

| Field          | Value                                                           |
| :------------- | :-------------------------------------------------------------- |
| Type           | `2`                                                             |
| Sequence Num   | Previous packet's `Seq + 1`                                     |
| Session ID     | Session identifier                                              |
| Payload Length | Number of bytes in this chunk; `1` to `1024`                    |
| Payload        | Raw binary file data (up to `MAX_PAYLOAD_SIZE = 1024` bytes)    |

The final DATA packet before EOF may carry fewer than 1024 bytes. The subsequent read from the file returns an empty bytes object, which triggers FIN transmission. The DATA sender stores the packet in `unacked_packet` and records `last_send_time = time.time()` immediately after dispatch.

---

#### Type 3 — ACK (Acknowledgment)

Sent by the receiver to confirm successful receipt of a DATA or FIN packet. The `Seq` field mirrors the sequence number of the packet being acknowledged. No payload is included.

| Field          | Value                                                |
| :------------- | :--------------------------------------------------- |
| Type           | `3`                                                  |
| Sequence Num   | Echoes the `Seq` of the packet being acknowledged    |
| Session ID     | Session identifier                                   |
| Payload Length | `0`                                                  |
| Payload        | Empty                                                |

ACKs are also re-sent (without advancing state) when a duplicate DATA or FIN packet is detected. The receiver identifies duplicates by comparing `packet.seq_num` against its current `expected_seq`. If `packet.seq_num < expected_seq` (server, UPLOAD) or `packet.seq_num < expected_seq` (client, DOWNLOAD), the receiver re-dispatches an ACK for the duplicate sequence without writing any data to disk.

---

#### Type 4 — FIN (End-of-Transfer Signal)

Sent by the data sender when it exhausts the source file. Signals that no further DATA packets will be transmitted for this session. Subject to the same Stop-and-Wait and retransmission rules as a DATA packet.

| Field          | Value                                                                        |
| :------------- | :--------------------------------------------------------------------------- |
| Type           | `4`                                                                          |
| Sequence Num   | `session['seq_num'] + 1` (server/DOWNLOAD) or `self.seq_num` (client/UPLOAD) |
| Session ID     | Session identifier                                                           |
| Payload Length | `0`                                                                          |
| Payload        | Empty                                                                        |

The server stores the FIN as `unacked_packet` and transitions session state to `'FIN_WAIT'`. The client enters the `send_fin()` retransmit loop. In both cases, a retransmission timer is active.

---

#### Type 5 — FIN-ACK (Reserved)

Defined in `protocol.py` as `TYPE_FIN_ACK = 5`. This type is **not generated** by either the client or the server in the current implementation. All FIN acknowledgments re-use Type 3 (ACK). Type 5 is reserved for potential future extension into a two-way FIN teardown sequence.

---

#### Type 6 — ERROR (Protocol Error Notification)

Sent exclusively by the server when a client-requested operation cannot be fulfilled. Carries a human-readable UTF-8 description of the error in the payload.

| Field          | Value                                                              |
| :------------- | :----------------------------------------------------------------- |
| Type           | `6`                                                                |
| Sequence Num   | Client's SYN `Seq + 1`                                             |
| Session ID     | Mirrored from the failing SYN                                      |
| Payload Length | Length of the error string                                         |
| Payload        | UTF-8 error description, e.g., `"File not found"`, `"Invalid SYN payload format"` |

The client detects an ERROR packet inside `connect()`. Upon receipt, it logs the error string, returns `False`, and does not open the output file. No session is created on the server side when ERROR is returned.

---

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

### 6.1 Checksum and Parsing Errors

If `Packet.from_bytes()` raises a `ValueError` (due to checksum mismatch or a datagram shorter than `HEADER_SIZE = 12` bytes), both the client and server log the exception and discard the packet silently:

- **Server:** Logs `[!] Checksum or parsing error from <addr>: <message>` inside `handle_packet()`; no response is sent.
- **Client:** Logs `[!] Parsing error: <message>` inside the receive loop; no response is sent.

No NACK mechanism exists. Packet loss due to corruption is handled identically to network loss: the sender's retransmission timer eventually fires and the packet is re-sent.

---

### 6.2 File Not Found (DOWNLOAD)

When a DOWNLOAD SYN arrives and `os.path.exists(filepath)` returns `False`, the server calls `send_error()` with the payload `b"File not found"`. An ERROR packet (Type 6) with `Seq = SYN.seq + 1` is dispatched to the client. No session entry is created. The file is never opened.

On the client, `connect()` detects `packet.msg_type == TYPE_ERROR`, prints the decoded error payload, and returns `False`. `download_file()` exits immediately without creating any output file.

---

### 6.3 Invalid SYN Payload Format

If the SYN payload cannot be split on `|` (i.e., `payload_str.split('|', 1)` raises `ValueError`), the server calls `send_error()` with `b"Invalid SYN payload format"`. No session is created. The client's behavior is identical to the File Not Found case.

---

### 6.4 Session ID Mismatch

Both client and server filter all received packets by `session_id`:

- **Client:** Every packet received in any loop is checked against `self.session_id`. Packets with a mismatched session ID are silently skipped without logging.
- **Server:** Non-SYN packets are looked up in `self.sessions`. If the `session_id` is absent, the server logs `[!] Received packet for unknown session <id>` and discards the packet without sending a response.

This prevents stray datagrams or re-ordered packets from terminated sessions from corrupting an active transfer.

---

### 6.5 Out-of-Order Packets (UPLOAD)

The protocol does not support selective acknowledgment (SACK) or receive-side buffering. In `handle_data()`, if `packet.seq_num > session['expected_seq']`, the server logs the discrepancy (`[!] Out of order packet seq X expected Y`) and discards the packet without writing data or sending an ACK. The client's retransmission timer will eventually re-send the in-order packet that caused the gap, after which the out-of-order packet must be re-sent by the client as well.

---

### 6.6 Stale Session Cleanup

`check_timeouts()` is invoked on every `socket.timeout` exception in the server's main `start()` loop (approximately every 2.0 seconds when the server is idle). For each session, it computes `elapsed = time.time() - session['last_send_time']`. If `elapsed > TIMEOUT × 5` (10.0 seconds), the session is classified as stale — the client is presumed crashed or unreachable. The server:

1. Calls `session['file_obj'].close()` inside a `try/except` to tolerate already-closed file handles.
2. Calls `del self.sessions[session_id]` to remove the session record.

**Important:** Stale session cleanup may leave a **partially written file** on disk in the `server_data/` directory. The current implementation performs no rollback, file deletion, or truncation of incomplete uploads or downloads. This is a known limitation.

---

### 6.7 Unknown Session Packets

If the server receives a non-SYN packet for a Session ID that is not present in `self.sessions` (e.g., after a stale cleanup or upon receiving a duplicate FIN for an already-closed session), it logs `[!] Received packet for unknown session <id>` and discards the packet. The server does **not** send an ERROR response in this case, as the counterpart client may be in a retransmit loop for a session that has already been destroyed.

---

### 6.8 Simulated Packet Loss (Testing Mechanism)

Both the client and server expose a configurable `drop_rate` attribute (default: `0.0`). When set to a value greater than `0.0`, each received packet is randomly discarded at the entry point of the receive path before parsing, with probability equal to `drop_rate`. This is exclusively a testing and simulation mechanism and is not part of the normative protocol specification. In deployment, `drop_rate` MUST be `0.0`.

---

## 7. File Transfer Operations

### 7.1 DOWNLOAD Operation (Server → Client)

In a DOWNLOAD session, the server reads the requested file from `server_data/<filename>` in sequential 1024-byte chunks and delivers each chunk to the client as a DATA packet under Stop-and-Wait discipline.

**Server-side operational sequence:**

1. `handle_syn()` receives a valid DOWNLOAD SYN. `os.path.basename(filename)` sanitizes the name. The resolved path is `os.path.join(SERVER_DIR, secure_filename)`.
2. `os.path.exists(filepath)` is checked. If the file is absent, an ERROR is returned and the function exits.
3. The file is opened for binary reading (`'rb'`). A session dictionary is created containing:
   - `state`: `'TRANSFERRING'`
   - `op`: `'DOWNLOAD'`
   - `file_obj`: The open file handle
   - `seq_num`: `SYN.seq + 1` (server's starting sequence counter)
   - `last_acked_seq`: `SYN.seq`
   - `unacked_packet`: `None`
   - `last_send_time`: `0`
4. SYN-ACK is dispatched to the client address.
5. `send_next_data()` is called immediately, without waiting for any client reply. It increments `session['seq_num']` by 1, reads up to 1024 bytes, constructs a DATA packet, sends it, stores it as `unacked_packet`, and records `last_send_time`.
6. On each subsequent ACK, `handle_ack()` validates the sequence number, clears `unacked_packet`, and calls `send_next_data()` again.
7. When `f.read()` returns `b""`, `send_next_data()` transitions to the FIN sequence (see Section 8).

**Client-side operational sequence:**

1. `download_file()` calls `connect("DOWNLOAD", filename)` to execute the SYN/SYN-ACK handshake.
2. On success, the output file is opened locally as `"downloaded_" + os.path.basename(filename)` in binary write mode (`'wb'`).
3. `expected_seq` is initialized to `self.seq_num + 2`.
4. The client enters a receive loop, validating each incoming packet's `session_id`, `msg_type`, and `seq_num`.
5. Valid in-order DATA packets are written to the output file and acknowledged.
6. Upon receiving a FIN, the client sends ACK and closes the output file (see Section 8).

---

### 7.2 UPLOAD Operation (Client → Server)

In an UPLOAD session, the client reads the local file in 1024-byte chunks and delivers each to the server as a DATA packet under Stop-and-Wait discipline.

**Client-side operational sequence:**

1. `upload_file()` verifies the local file exists with `os.path.exists(filename)`. If not, it logs an error and returns.
2. `connect("UPLOAD", os.path.basename(filename))` sends SYN and waits for SYN-ACK.
3. On success, `self.seq_num` is incremented by 1 (to `N+1`) to set the sequence number for the first DATA packet.
4. The file is opened for binary reading (`'rb'`). Chunks of up to 1024 bytes are read in sequence.
5. For each non-empty chunk, a DATA packet is constructed and the inner Stop-and-Wait loop executes: send → wait for ACK → retransmit on timeout → advance `seq_num` on success.
6. When `f.read()` returns `b""`, `send_fin()` is called to initiate termination.

**Server-side operational sequence:**

1. `handle_syn()` receives a valid UPLOAD SYN. The destination path is `os.path.join(SERVER_DIR, os.path.basename(filename))`. If a file of the same name already exists, it is **overwritten without warning**.
2. The file is opened for binary writing (`'wb'`). A session dictionary is created containing:
   - `state`: `'TRANSFERRING'`
   - `op`: `'UPLOAD'`
   - `file_obj`: The open file handle
   - `expected_seq`: `SYN.seq + 1`
3. SYN-ACK is dispatched to the client.
4. Incoming DATA packets are handled in `handle_data()`. In-order packets are written and ACKed; duplicates are re-ACKed without being written; out-of-order packets are discarded.
5. On FIN receipt, `handle_fin()` sends ACK, closes the file, and destroys the session.

---

### 7.3 Filename Handling and Path Security

The server applies `os.path.basename()` to every filename received in a SYN payload before constructing any filesystem path. This strips all directory components, including:

- Relative path traversal sequences (`../../etc/passwd` becomes `passwd`)
- Absolute path prefixes (`/etc/shadow` becomes `shadow`)

All file I/O is constrained to the `server_data/` directory. The server does not validate for special characters within the sanitized filename beyond what the host OS enforces.

---

### 7.4 Chunk Sizing Reference

| Parameter              | Value                                              |
| :--------------------- | :------------------------------------------------- |
| `MAX_PAYLOAD_SIZE`     | `1024` bytes                                       |
| `HEADER_SIZE`          | `12` bytes                                         |
| Maximum UDP datagram   | `1036` bytes                                       |
| Final chunk size       | `1` to `1024` bytes (variable); `0` triggers FIN  |
| Chunks per `f.read()`  | One read of up to `MAX_PAYLOAD_SIZE` per loop iteration |

---

## 8. End-of-File Signaling

End-of-file is signaled by the data sender (server for DOWNLOAD, client for UPLOAD) using a FIN packet (Type 4). The FIN packet is subject to the same Stop-and-Wait and retransmission rules as a DATA packet.

### 8.1 FIN Sequence — DOWNLOAD (Server Initiates Termination)

The server detects EOF when `f.read(MAX_PAYLOAD_SIZE)` returns an empty bytes object inside `send_next_data()`.

**FIN dispatch and acknowledgment sequence:**

1. The server increments `session['seq_num']` by 1 and constructs `FIN(Seq = session['seq_num'], Session=S)`.
2. The FIN is transmitted to the client.
3. Session state is set to `'FIN_WAIT'`. The FIN packet is stored as `session['unacked_packet']` and `session['last_send_time']` is recorded.
4. The client's receive loop detects `msg_type == TYPE_FIN`. It sends `ACK(Seq=FIN.seq, Session=S)` and closes the output file. The download loop exits.
5. The server's `handle_ack()` detects `session['state'] == 'FIN_WAIT'` and that the ACK's `seq_num` matches `unacked_packet.seq_num`. It calls `session['file_obj'].close()` and `del self.sessions[session_id]`.

**FIN loss:** `check_timeouts()` retransmits the FIN after 2.0 seconds. The client re-receives and re-sends the ACK without re-writing the output file.

**FIN-ACK loss:** The server retransmits the FIN from `FIN_WAIT`. The client re-sends the ACK. If the client has already exited its receive loop, the re-sent ACK may be lost and the server's stale session cleanup will eventually close the session after 10 seconds.

---

### 8.2 FIN Sequence — UPLOAD (Client Initiates Termination)

The client detects EOF when `f.read(MAX_PAYLOAD_SIZE)` returns `b""` inside `upload_file()`.

**FIN dispatch and acknowledgment sequence:**

1. The client's chunk loop exits and `send_fin()` is called.
2. `send_fin()` constructs `FIN(Seq = self.seq_num, Session=S)` and enters a `while True` retransmit loop, blocking on `sock.recvfrom()` with a 2.0-second timeout.
3. The server's `handle_fin()` is invoked. It sends `ACK(Seq=FIN.seq, Session=S)`, calls `session['file_obj'].close()`, and calls `del self.sessions[session_id]`.
4. The client's `send_fin()` receives the ACK and validates `session_id`, `msg_type == TYPE_ACK`, and `seq_num == self.seq_num`. On a valid match, it logs upload-complete and exits the loop.

**FIN loss:** The 2.0-second socket timeout in `send_fin()` fires and the FIN is retransmitted.

**FIN-ACK loss (known limitation):** If the server's ACK for the FIN is lost, the client retransmits the FIN. At this point, the server has already destroyed the session. The server logs `[!] Received packet for unknown session <id>` and discards the duplicate FIN without re-sending an ACK. Because `send_fin()` has no maximum retry count or teardown timeout, the client will **retransmit indefinitely** in this scenario. Manual process termination is required.

---

### 8.3 FIN Packet Field Comparison

| Field        | DOWNLOAD (Server Initiates)                        | UPLOAD (Client Initiates)                           |
| :----------- | :------------------------------------------------- | :-------------------------------------------------- |
| Sender       | Server                                             | Client                                              |
| Seq Number   | `session['seq_num'] + 1` (incremented in-place)    | `self.seq_num` (current value at time of EOF read)  |
| Payload      | Empty (`b""`)                                      | Empty (`b""`)                                       |
| Stored As    | `session['unacked_packet']`                        | Local variable inside `send_fin()`                  |
| Retransmit   | `check_timeouts()` polls every ~2.0s               | `socket.timeout` exception in `send_fin()` loop     |
| ACK Sender   | Client                                             | Server                                              |
| On ACK       | File closed; session deleted from `self.sessions`  | Client exits `send_fin()` loop; prints completion   |
