import { processJob } from "../lib/ai.mjs";
console.log("Involta worker: очередь активна");
let stop = false;
process.on("SIGTERM", () => {
  stop = true;
});
process.on("SIGINT", () => {
  stop = true;
});
while (!stop) {
  try {
    if (!(await processJob())) await new Promise((r) => setTimeout(r, 2000));
  } catch (e) {
    console.error(e.message);
    await new Promise((r) => setTimeout(r, 2000));
  }
}
