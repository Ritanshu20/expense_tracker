// Dashboard: draws ranked bars for spending by category and by payment method.
// The table in the same card holds the same numbers, so nothing depends on this file.
document.addEventListener("DOMContentLoaded", function () {
  const styles = getComputedStyle(document.documentElement);
  const barColor = styles.getPropertyValue("--bs-primary").trim();
  const gridColor = styles.getPropertyValue("--bs-border-color").trim();
  const textColor = styles.getPropertyValue("--bs-secondary-color").trim();
  const heightPerBar = 34;
  const heightOfAxis = 30;
  const compactNumber = new Intl.NumberFormat("en-IN", { notation: "compact" });

  function drawRankedBars(chartId, dataId) {
    const canvas = document.getElementById(chartId);
    if (!canvas) {
      return;
    }
    const rows = JSON.parse(document.getElementById(dataId).textContent);
    // Chart.js sizes itself to its parent, so the parent grows with the number of bars.
    canvas.parentElement.style.height = rows.length * heightPerBar + heightOfAxis + "px";

    new Chart(canvas, {
      type: "bar",
      data: {
        labels: rows.map(function (row) { return row.label; }),
        datasets: [{
          // The amounts come as exact strings. The numbers are only used for bar lengths.
          data: rows.map(function (row) { return Number(row.total); }),
          backgroundColor: barColor,
          maxBarThickness: 24,
          borderRadius: { topRight: 4, bottomRight: 4 },
          borderSkipped: false,
        }],
      },
      options: {
        indexAxis: "y",
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (context) { return rows[context.dataIndex].total; },
            },
          },
        },
        scales: {
          x: {
            beginAtZero: true,
            grid: { color: gridColor },
            border: { display: false },
            ticks: {
              color: textColor,
              maxTicksLimit: 5,
              callback: function (value) { return compactNumber.format(value); },
            },
          },
          y: {
            grid: { display: false },
            border: { color: gridColor },
            ticks: { color: textColor },
          },
        },
      },
    });
  }

  drawRankedBars("category-chart", "category-totals-data");
  drawRankedBars("payment-method-chart", "payment-method-totals-data");
});
