// Match each week's day heights to its tallest row, including wrapped names.
// Recalculate after signup/discard changes and viewport/font size changes.
(() => {
  document.querySelectorAll('.sched-table').forEach(table => {
    const body = table.tBodies[0];
    if (!body) return;
    let frame;
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const rows = Array.from(body.rows);
        rows.forEach(row => row.style.height = '');
        const height = Math.ceil(Math.max(0, ...rows.map(row => row.getBoundingClientRect().height)));
        rows.forEach(row => row.style.height = `${height}px`);
      });
    };
    let width = 0;
    new ResizeObserver(entries => {
      const next = entries[0].contentRect.width;
      if (next !== width) { width = next; schedule(); }
    }).observe(table);
    new MutationObserver(schedule).observe(body, {childList: true, subtree: true, characterData: true});
    window.addEventListener('resize', schedule);
    document.fonts.ready.then(schedule);
    schedule();
  });
})();
