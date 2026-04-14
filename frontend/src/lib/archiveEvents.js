export function subscribeArchiveCompletion(onComplete) {
  if (typeof window === "undefined" || typeof EventSource === "undefined") {
    return () => {};
  }

  const eventSource = new EventSource("/api/events/archive");

  const handleArchiveCompleted = (event) => {
    try {
      const payload = JSON.parse(event.data || "{}");
      onComplete(payload);
    } catch (error) {
      console.error("归档完成事件解析失败", error);
    }
  };

  eventSource.addEventListener("archive_completed", handleArchiveCompleted);

  return () => {
    eventSource.removeEventListener("archive_completed", handleArchiveCompleted);
    eventSource.close();
  };
}
