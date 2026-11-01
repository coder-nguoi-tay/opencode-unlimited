#!/usr/bin/env node
/**
 * Smart Proxy Bridge for OpenCode (Robust Production Edition)
 * Lắng nghe tại 127.0.0.1:28080 -> Upstream Proxy 160.250.166.20:10427
 *
 * Tính năng chống crash & chống đứt gãy:
 * 1. Bắt toàn bộ ngoại lệ uncaughtException / unhandledRejection để không bao giờ bị sập process.
 * 2. Tự động gắn error listener cho cả clientSocket và upstreamSocket từ millisecond đầu tiên.
 * 3. Hỗ trợ HOLD request khi phát hiện cờ /tmp/proxy_rotating (đợi 60s an toàn).
 * 4. Tự động dọn dẹp socket khi một trong 2 đầu đóng kết nối.
 */

import http from 'http';
import net from 'net';
import fs from 'fs';
import { pipeline } from 'stream';

const LISTEN_HOST = '127.0.0.1';
const LISTEN_PORT = 28080;
const UPSTREAM_HOST = '160.250.166.20';
const UPSTREAM_PORT = 10427;
const ROTATING_FLAG_FILE = '/tmp/proxy_rotating';
const MAX_WAIT_MS = 90000;

// 1. Safety net toàn cục - Tuyệt đối không để Node process bị crash
process.on('uncaughtException', (err) => {
  console.log(`[SmartBridge Caught Exception] ${err.code || err.message}`);
});

process.on('unhandledRejection', (reason) => {
  console.log(`[SmartBridge Unhandled Rejection]`, reason);
});

function isRotating() {
  try {
    return fs.existsSync(ROTATING_FLAG_FILE);
  } catch {
    return false;
  }
}

async function waitForRotationComplete(clientSocket) {
  const startTime = Date.now();
  while (isRotating()) {
    if (clientSocket && (clientSocket.destroyed || !clientSocket.writable)) return false;
    if (Date.now() - startTime > MAX_WAIT_MS) {
      console.log(`[SmartBridge] ⚠️ Quá thời gian đợi xoay IP (${MAX_WAIT_MS}ms)`);
      return false;
    }
    await new Promise(r => setTimeout(r, 1000));
  }
  return true;
}

// Kết nối tới upstream có retry tự động
async function connectToUpstreamWithRetry(clientSocket) {
  const startTime = Date.now();
  while (Date.now() - startTime < MAX_WAIT_MS) {
    if (clientSocket && (clientSocket.destroyed || !clientSocket.writable)) return null;

    if (isRotating()) {
      const ok = await waitForRotationComplete(clientSocket);
      if (!ok) return null;
    }

    try {
      const socket = await new Promise((resolve, reject) => {
        const s = net.connect(UPSTREAM_PORT, UPSTREAM_HOST);
        const onError = (err) => {
          s.destroy();
          cleanup();
          reject(err);
        };
        const onTimeout = () => {
          s.destroy();
          cleanup();
          reject(new Error('Connect timeout'));
        };
        const onConnect = () => {
          cleanup();
          resolve(s);
        };
        function cleanup() {
          s.removeListener('error', onError);
          s.removeListener('timeout', onTimeout);
          s.removeListener('connect', onConnect);
        }
        s.setTimeout(12000);
        s.once('error', onError);
        s.once('timeout', onTimeout);
        s.once('connect', onConnect);
      });
      return socket;
    } catch (err) {
      console.log(`[SmartBridge] Upstream chưa sẵn sàng (${err.message}), thử lại sau 2s...`);
      await new Promise(r => setTimeout(r, 2000));
    }
  }
  return null;
}

const server = http.createServer(async (req, clientRes) => {
  clientRes.on('error', () => {});
  req.on('error', () => {});

  await waitForRotationComplete(clientRes.socket);

  const options = {
    hostname: UPSTREAM_HOST,
    port: UPSTREAM_PORT,
    path: req.url,
    method: req.method,
    headers: req.headers
  };

  const proxyReq = http.request(options, (upstreamRes) => {
    upstreamRes.on('error', () => {});
    clientRes.writeHead(upstreamRes.statusCode, upstreamRes.headers);
    pipeline(upstreamRes, clientRes, () => {});
  });

  proxyReq.on('error', (err) => {
    if (!clientRes.headersSent) {
      clientRes.writeHead(502, { 'Content-Type': 'text/plain' });
      clientRes.end('Upstream proxy error: ' + err.message);
    }
  });

  pipeline(req, proxyReq, () => {});
});

// Xử lý HTTPS CONNECT (OpenCode AI API, Claude, ChatGPT...)
server.on('connect', async (req, clientSocket, head) => {
  console.log(`[SmartBridge CONNECT] ${req.url} (từ ${clientSocket.remoteAddress}:${clientSocket.remotePort})`);
  // Gắn error listener ngay lập tức để không bao giờ bị unhandled ECONNRESET
  clientSocket.on('error', (err) => {
    // client ngắt kết nối bình thường, không cần throw
  });

  const upstreamSocket = await connectToUpstreamWithRetry(clientSocket);
  if (!upstreamSocket) {
    if (!clientSocket.destroyed) {
      clientSocket.write('HTTP/1.1 504 Gateway Timeout\r\n\r\nProxy unavailable');
      clientSocket.end();
    }
    return;
  }

  upstreamSocket.on('error', (err) => {
    // upstream ngắt kết nối
    if (!clientSocket.destroyed) clientSocket.destroy();
  });

  upstreamSocket.setTimeout(0);
  clientSocket.setTimeout(0);

  // Gửi lệnh CONNECT tới upstream proxy
  upstreamSocket.write(
    `${req.method} ${req.url} HTTP/${req.httpVersion}\r\n` +
    Object.keys(req.headers).map(k => `${k}: ${req.headers[k]}`).join('\r\n') +
    '\r\n\r\n'
  );
  if (head && head.length) upstreamSocket.write(head);

  let handshakeDone = false;
  let buffer = Buffer.alloc(0);

  const onData = (chunk) => {
    if (!handshakeDone) {
      buffer = Buffer.concat([buffer, chunk]);
      const idx = buffer.indexOf('\r\n\r\n');
      if (idx !== -1) {
        handshakeDone = true;
        upstreamSocket.removeListener('data', onData);

        const responseHeader = buffer.slice(0, idx + 4);
        const rest = buffer.slice(idx + 4);

        if (!clientSocket.destroyed) {
          clientSocket.write(responseHeader);
          if (rest.length) clientSocket.write(rest);

          // Dùng pipeline an toàn cho 2 chiều
          pipeline(upstreamSocket, clientSocket, () => {});
          pipeline(clientSocket, upstreamSocket, () => {});
        }
      }
    }
  };

  upstreamSocket.on('data', onData);
});

server.on('clientError', (err, socket) => {
  socket.destroy();
});

server.listen(LISTEN_PORT, LISTEN_HOST, () => {
  console.log(`[SmartBridge] Đang lắng nghe tại http://${LISTEN_HOST}:${LISTEN_PORT} -> upstream ${UPSTREAM_HOST}:${UPSTREAM_PORT}`);
});
