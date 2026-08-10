const SHUTDOWN_PATH = "/api/v1/control/shutdown";

export async function confirmAndShutdown({
  confirmShutdown,
  fetchImpl,
  onStarting,
  onComplete,
}) {
  if (!confirmShutdown()) {
    return { status: "cancelled" };
  }

  onStarting();
  let response;
  try {
    response = await fetchImpl(SHUTDOWN_PATH, { method: "POST" });
  } catch {
    onComplete();
    return { status: "disconnected" };
  }

  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.text()).trim();
    } catch {
      // The HTTP status is sufficient when the response body cannot be read.
    }
    const suffix = detail ? `: ${detail}` : "";
    throw new Error(`关闭服务请求失败（HTTP ${response.status}）${suffix}`);
  }

  onComplete();
  return { status: "accepted" };
}
