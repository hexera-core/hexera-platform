// Console revamp mockups - a made-up demo part (no customer data): a block with a round boss
// and a bore, drawn in the new mid-tone, low-glare part colour. `data-mesh` adds a surface mesh.
const mesh = (id) => `<pattern id="${id}" width="18" height="10.4" patternUnits="userSpaceOnUse">
  <path d="M0 0L9 5.2L18 0M0 10.4L9 5.2L18 10.4M9 5.2V-1M0 0V10.4" fill="none" stroke="#c9d3df" stroke-opacity=".42" stroke-width=".7"/></pattern>`;
document.querySelectorAll("[data-part]").forEach((el) => {
  const m = el.hasAttribute("data-mesh");
  const hi = el.dataset.part === "hi";   // one patch picked: the selection accent on the bore
  const ov = (d) => m ? `<path d="${d}" fill="url(#mz)"/>` : "";
  const top = "M280 120L460 210L280 300L100 210Z", left = "M100 210L280 300L280 430L100 340Z",
        right = "M280 300L460 210L460 340L280 430Z",
        boss = "M210 210V150A70 35 0 0 1 350 150V210A70 35 0 0 1 210 210Z";
  el.innerHTML = `<svg class="part" viewBox="60 60 440 400" aria-label="Demo part">
    <defs>${mesh("mz")}
      <linearGradient id="bs" x1="0" x2="1"><stop offset="0" stop-color="var(--part-dark)"/>
        <stop offset=".45" stop-color="var(--part-body)"/><stop offset="1" stop-color="var(--part-shade)"/></linearGradient>
      <radialGradient id="tp" cx=".35" cy=".3" r=".9"><stop offset="0" stop-color="#868c94"/><stop offset="1" stop-color="var(--part-body)"/></radialGradient></defs>
    <ellipse cx="280" cy="440" rx="200" ry="26" fill="#000" opacity=".35"/>
    <path d="${left}" fill="var(--part-shade)"/>${ov(left)}
    <path d="${right}" fill="var(--part-dark)"/>${ov(right)}
    <path d="${top}" fill="url(#tp)"/>${ov(top)}
    <g fill="none" stroke="var(--part-edge)" stroke-width="1.2" stroke-linejoin="round"><path d="${top}"/>
      <path d="M100 210V340L280 430L460 340V210M280 300V430"/></g>
    <path d="${boss}" fill="url(#bs)"/>${ov(boss)}
    <ellipse cx="280" cy="150" rx="70" ry="35" fill="#878d95"/>${m ? `<ellipse cx="280" cy="150" rx="70" ry="35" fill="url(#mz)"/>` : ""}
    <ellipse cx="280" cy="152" rx="34" ry="17" fill="${hi ? "var(--accent)" : "#3a3e44"}" ${hi ? 'fill-opacity=".85"' : ""}/>
    <g fill="none" stroke="var(--part-edge)" stroke-width="1.2" stroke-linejoin="round">
      <path d="M210 210V150M350 150V210"/><ellipse cx="280" cy="150" rx="70" ry="35"/>
      <path d="M210 210A70 35 0 0 0 350 210"/></g>
    <ellipse cx="190" cy="318" rx="9" ry="16" transform="rotate(-26.6 190 318)" fill="#3f444a"/>
    <ellipse cx="370" cy="318" rx="9" ry="16" transform="rotate(26.6 370 318)" fill="#32363c"/>
  </svg>`;
});
