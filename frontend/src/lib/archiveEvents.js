import { subscribeStateEvents } from "./stateEvents";


export function subscribeArchiveCompletion(onComplete) {
  if (typeof onComplete !== "function") {
    return () => {};
  }

  return subscribeStateEvents((event) => {
    const type = event?.type || "";
    if (!["archive.completed", "organize.completed"].includes(type)) {
      return;
    }
    const payload = event?.payload || {};
    onComplete({
      completed: [
        {
          id: payload.id || "",
          url: payload.url || "",
          title: payload.title || "",
          kind: type === "organize.completed" ? "local_organize" : "archive",
        },
      ],
      event,
    });
  }, ["archive_queue", "organize_tasks"]);
}
