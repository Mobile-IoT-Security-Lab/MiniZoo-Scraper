// wxappUnpacker can save one output path from multiple reconstruction stages.
// Preserve its callback API while serializing complete writes to the same file.
const fs = require('fs');
const path = require('path');
const originalWriteFile = fs.writeFile;
const pending = new Map();

fs.writeFile = function (...args) {
  const callback = args[args.length - 1];
  if (typeof args[0] !== 'string' || typeof callback !== 'function') {
    return originalWriteFile.apply(fs, args);
  }
  let key = path.resolve(args[0]);
  if (process.platform === 'darwin' || process.platform === 'win32') {
    key = key.normalize('NFC').toLowerCase();
  }
  const queue = pending.get(key) || [];
  pending.set(key, queue);
  const write = () => originalWriteFile.apply(fs, [
    ...args.slice(0, -1),
    (error) => {
      try {
        callback(error);
      } finally {
        queue.shift();
        if (queue.length) queue[0]();
        else pending.delete(key);
      }
    },
  ]);
  queue.push(write);
  if (queue.length === 1) write();
};
