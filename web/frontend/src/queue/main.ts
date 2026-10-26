// main.ts — the standalone GPU Queue page (/queue/). The view itself lives in
// view.ts so the review screen can show it in place too.

import { type Overview, QueueView } from "./view";

const host = document.getElementById("queue-page");
if (host) {
  const status = document.getElementById("queue-status");
  const view = new QueueView({
    isStaff: host.dataset.isStaff === "true",
    csrfToken: document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "",
    onStatus: (message) => {
      if (status) status.textContent = message;
    },
  });
  host.replaceChildren(view.root);
  const initial = document.getElementById("queue-initial")?.textContent;
  view.show(initial ? (JSON.parse(initial) as Overview) : undefined);
}
