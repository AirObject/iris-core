import { copyFile, mkdir } from 'node:fs/promises';
await mkdir('dist', { recursive: true });
for (const file of ['index.html', 'style.css']) {
  await copyFile(`public/${file}`, `dist/${file}`);
}

await mkdir('dist/protocol', { recursive: true });
for (const file of ['http.json', 'ws.json']) await copyFile('../protocol/' + file, 'dist/protocol/' + file);
for (const file of ['iris_client.py', 'http_client.py', 'reconnect.py']) await copyFile('../clients/' + file, 'dist/protocol/' + file);
for (const file of ['event.json', 'query.json']) await copyFile('../clients/examples/' + file, 'dist/protocol/' + file);
