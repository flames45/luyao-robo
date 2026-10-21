# Bundled web dependencies

These files are served locally so that the application does not require a public CDN to start.

- GSAP 3.12.2: https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.2/gsap.min.js — original license notice retained in the JS header; https://gsap.com/standard-license/
- Font Awesome Free 6.6.0: CSS and WOFF2 fonts from https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/ — original attribution retained in the CSS; fonts SIL OFL 1.1, code MIT, icons CC BY 4.0; https://fontawesome.com/license/free
- Tailwind CSS 3.4.17: compiled from `tailwind.input.css` against `index.html` using the MIT-licensed Tailwind CLI; https://github.com/tailwindlabs/tailwindcss/blob/v3.4.17/LICENSE

The Live2D runtime and model in `live2d/` are the project's existing assets, reused without a character redesign. `assets/pet-fallback.png` is a raster rendering of that same model for browsers where WebGL/model loading fails.
