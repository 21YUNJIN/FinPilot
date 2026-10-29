/**
 * 把非 TS 资源（styles.css）复制到 dist。
 * 说明：tsc 不会搬运 CSS；两个客户端虽然通过别名直接引用 src，但 npm 消费方需要 dist 里有样式。
 */
import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const from = resolve(root, "src/styles.css");
const to = resolve(root, "dist/styles.css");

await mkdir(dirname(to), { recursive: true });
await copyFile(from, to);
console.log(`[shared-ui] copied ${from} -> ${to}`);