import { chmodSync, writeFileSync } from "node:fs";

writeFileSync(
  new URL("../dist/cjs/package.json", import.meta.url),
  '{"type":"commonjs"}\n',
);
chmodSync(new URL("../dist/esm/cli.js", import.meta.url), 0o755);
