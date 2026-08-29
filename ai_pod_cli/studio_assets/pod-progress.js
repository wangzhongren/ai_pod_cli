/* Keep the cancellation handler reusable after a failed or cancelled build. */
document.getElementById("buildPod").addEventListener("click", () => {
  queueMicrotask(() => {
    const cancel = document.getElementById("cancelPod");
    cancel.disabled = false;
    cancel.onclick = async () => {
      if (podBuildId) {
        cancel.disabled = true;
        await invoke("cancel_pod_build", podBuildId);
        cancel.disabled = false;
        cancel.textContent = "Cancelling…";
        return;
      }
      podDialog.close();
    };
  });
});
