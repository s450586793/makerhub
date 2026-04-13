import { createApp } from "vue";

import App from "./App.vue";
import router from "./router";
import "./style.css";
import { applyTheme, getStoredThemePreference, startThemeObserver } from "./lib/theme";


applyTheme(getStoredThemePreference());
startThemeObserver();

createApp(App).use(router).mount("#app");
