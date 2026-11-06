// Expense form: shows the participant section for shared expenses, and adds or removes rows.
// The server checks everything again, so this file only makes the form easier to use.
document.addEventListener("DOMContentLoaded", function () {
  const expenseType = document.getElementById("id_expense_type");
  const splitType = document.getElementById("id_split_type");
  const sharedSection = document.getElementById("shared-section");
  const rows = document.getElementById("participant-rows");
  const emptyRow = document.getElementById("empty-participant-row");
  const totalForms = document.getElementById("id_participants-TOTAL_FORMS");
  const shareLabels = { custom: "Share amount", percentage: "Percent" };

  // Django expects rows numbered 0, 1, 2... with no gaps, and the total in TOTAL_FORMS.
  function numberRows() {
    const rowElements = rows.querySelectorAll(".participant-row");
    rowElements.forEach(function (row, index) {
      row.querySelectorAll("input, label").forEach(function (element) {
        ["name", "id", "for"].forEach(function (attribute) {
          const value = element.getAttribute(attribute);
          if (value) {
            const numbered = value.replace(/participants-(\d+|__prefix__)-/, "participants-" + index + "-");
            element.setAttribute(attribute, numbered);
          }
        });
      });
    });
    totalForms.value = rowElements.length;
  }

  function updateVisibleFields() {
    sharedSection.hidden = expenseType.value !== "shared";
    const shareLabel = shareLabels[splitType.value];
    rows.querySelectorAll(".share-field").forEach(function (field) {
      field.hidden = !shareLabel;
      if (shareLabel) {
        field.querySelector("label").textContent = shareLabel + ":";
      }
    });
  }

  document.getElementById("add-participant").addEventListener("click", function () {
    rows.appendChild(emptyRow.content.cloneNode(true));
    numberRows();
    updateVisibleFields();
  });

  rows.addEventListener("click", function (event) {
    if (event.target.classList.contains("remove-participant")) {
      event.target.closest(".participant-row").remove();
      numberRows();
    }
  });

  expenseType.addEventListener("change", updateVisibleFields);
  splitType.addEventListener("change", updateVisibleFields);
  updateVisibleFields();
});
