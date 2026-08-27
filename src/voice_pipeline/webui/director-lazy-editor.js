export function syncLazyEditor({ open, mounted, mount, unmount }) {
  if (open && mounted === null) return mount();
  if (!open && mounted !== null) {
    unmount(mounted);
    return null;
  }
  return mounted;
}
