// import.meta.env.BASE_URL is set by Vite to the 'base' config value.
// In dev (base '/') → ''. In production with VITE_BASE=/a/ → '/a'.
// All frontend fetch calls must prefix API paths with this value.
export const apiBase = import.meta.env.BASE_URL.replace(/\/+$/, '');
