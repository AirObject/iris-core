import { copyFile, mkdir } from 'node:fs/promises';
await mkdir('dist', { recursive: true });
for (const file of ['index.html', 'style.css']) {
  await copyFile(`public/${file}`, `dist/${file}`);
}
