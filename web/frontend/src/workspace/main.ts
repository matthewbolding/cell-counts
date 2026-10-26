// main.ts — entry point for the review screen (templates/projects/workspace.html).

import "./workspace.css";
import { errorMessage, get } from "./api";
import type { Bootstrap } from "./types";
import { Workspace } from "./workspace";

async function start(): Promise<void> {
  const host = document.getElementById("workspace");
  if (!host) return;
  const d = host.dataset;
  const csrfToken = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
  try {
    const boot = await get<Bootstrap>(`/api/projects/${d.projectId}/`);
    new Workspace(host, {
      projectId: d.projectId!,
      projectsUrl: d.projectsUrl!,
      settingsUrl: d.settingsUrl!,
      accountSettingsUrl: d.accountSettingsUrl!,
      logoutUrl: d.logoutUrl!,
      manageUrl: d.manageUrl ?? "",
      messages: d.messages ?? "",
      csrfToken,
    }, boot);
  } catch (err) {
    host.textContent = `Couldn't open this project: ${errorMessage(err)}`;
    host.classList.add("ws-failed");
  }
}

void start();
