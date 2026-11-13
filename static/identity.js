"use strict";

const identitySelect = document.getElementById("demo-user");
identitySelect.addEventListener("change", () => identitySelect.form.requestSubmit());
