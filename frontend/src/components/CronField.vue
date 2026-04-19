<template>
  <div ref="rootRef" class="cron-field">
    <div class="cron-field__row">
      <input
        class="cron-field__input"
        :value="modelValue"
        type="text"
        :placeholder="placeholder"
        @focus="openPopover"
        @click="openPopover"
        @input="handleTextInput"
      >
    </div>
    <small class="cron-field__summary">{{ cronDescription }}</small>

    <div
      v-if="popoverVisible"
      class="cron-popover"
      role="dialog"
      aria-modal="false"
      :aria-label="dialogTitle"
    >
      <div class="cron-dialog__header">
        <div>
          <span class="eyebrow">Cron</span>
          <h2>{{ dialogTitle }}</h2>
        </div>
        <small class="cron-dialog__hint">点击外部区域即可收起</small>
      </div>

      <div class="cron-dialog__modes">
        <button
          v-for="option in modeOptions"
          :key="option.value"
          :class="['cron-dialog__mode', draft.mode === option.value && 'is-active']"
          type="button"
          @click="selectMode(option.value)"
        >
          {{ option.label }}
        </button>
      </div>

      <div class="cron-dialog__content">
        <div v-if="draft.mode === 'every_minute'" class="cron-dialog__block">
          <p class="cron-dialog__note">每分钟执行一次，不限制小时、日期或星期。</p>
        </div>

        <div v-else-if="draft.mode === 'every_minutes'" class="cron-dialog__grid">
          <label class="field-card">
            <span>间隔分钟</span>
            <input v-model.number="draft.minuteInterval" type="number" min="1" max="59" @input="applyPresetCron">
          </label>
        </div>

        <div v-else-if="draft.mode === 'hourly'" class="cron-dialog__grid">
          <label class="field-card">
            <span>分钟</span>
            <input v-model.number="draft.minute" type="number" min="0" max="59" @input="applyPresetCron">
          </label>
        </div>

        <div v-else-if="draft.mode === 'every_hours'" class="cron-dialog__grid cron-dialog__grid--two">
          <label class="field-card">
            <span>分钟</span>
            <input v-model.number="draft.minute" type="number" min="0" max="59" @input="applyPresetCron">
          </label>
          <label class="field-card">
            <span>间隔小时</span>
            <input v-model.number="draft.hourInterval" type="number" min="1" max="23" @input="applyPresetCron">
          </label>
        </div>

        <div v-else-if="draft.mode === 'daily'" class="cron-dialog__grid cron-dialog__grid--two">
          <label class="field-card">
            <span>小时</span>
            <input v-model.number="draft.hour" type="number" min="0" max="23" @input="applyPresetCron">
          </label>
          <label class="field-card">
            <span>分钟</span>
            <input v-model.number="draft.minute" type="number" min="0" max="59" @input="applyPresetCron">
          </label>
        </div>

        <div v-else-if="draft.mode === 'weekly'" class="cron-dialog__grid cron-dialog__grid--three">
          <label class="field-card">
            <span>星期</span>
            <select v-model="draft.weekday" @change="applyPresetCron">
              <option v-for="item in weekdayOptions" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </label>
          <label class="field-card">
            <span>小时</span>
            <input v-model.number="draft.hour" type="number" min="0" max="23" @input="applyPresetCron">
          </label>
          <label class="field-card">
            <span>分钟</span>
            <input v-model.number="draft.minute" type="number" min="0" max="59" @input="applyPresetCron">
          </label>
        </div>

        <div v-else-if="draft.mode === 'monthly'" class="cron-dialog__grid cron-dialog__grid--three">
          <label class="field-card">
            <span>每月日期</span>
            <input v-model.number="draft.day" type="number" min="1" max="31" @input="applyPresetCron">
          </label>
          <label class="field-card">
            <span>小时</span>
            <input v-model.number="draft.hour" type="number" min="0" max="23" @input="applyPresetCron">
          </label>
          <label class="field-card">
            <span>分钟</span>
            <input v-model.number="draft.minute" type="number" min="0" max="59" @input="applyPresetCron">
          </label>
        </div>

        <div v-else-if="draft.mode === 'yearly'" class="cron-dialog__grid cron-dialog__grid--four">
          <label class="field-card">
            <span>月份</span>
            <select v-model="draft.month" @change="applyPresetCron">
              <option v-for="item in monthOptions" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </label>
          <label class="field-card">
            <span>日期</span>
            <input v-model.number="draft.day" type="number" min="1" max="31" @input="applyPresetCron">
          </label>
          <label class="field-card">
            <span>小时</span>
            <input v-model.number="draft.hour" type="number" min="0" max="23" @input="applyPresetCron">
          </label>
          <label class="field-card">
            <span>分钟</span>
            <input v-model.number="draft.minute" type="number" min="0" max="59" @input="applyPresetCron">
          </label>
        </div>

        <div v-else class="cron-dialog__block">
          <p class="cron-dialog__note">直接在上方输入框里编辑原始 Cron 表达式，下面会实时显示解析结果。</p>
        </div>

        <div class="cron-dialog__preview">
          <strong>{{ previewCron || "未设置" }}</strong>
          <p>{{ previewDescription }}</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";


const props = defineProps({
  modelValue: {
    type: String,
    default: "",
  },
  placeholder: {
    type: String,
    default: "Cron，例如 0 */6 * * *",
  },
  dialogTitle: {
    type: String,
    default: "设置 Cron",
  },
});

const emit = defineEmits(["update:modelValue"]);

const rootRef = ref(null);
const popoverVisible = ref(false);
const customCron = ref("");
const modeOptions = [
  { value: "every_minute", label: "每分钟" },
  { value: "every_minutes", label: "每隔 N 分钟" },
  { value: "hourly", label: "每小时" },
  { value: "every_hours", label: "每隔 N 小时" },
  { value: "daily", label: "每天" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
  { value: "yearly", label: "每年" },
  { value: "custom", label: "自定义" },
];
const weekdayOptions = [
  { value: "0", label: "周日" },
  { value: "1", label: "周一" },
  { value: "2", label: "周二" },
  { value: "3", label: "周三" },
  { value: "4", label: "周四" },
  { value: "5", label: "周五" },
  { value: "6", label: "周六" },
];
const monthOptions = Array.from({ length: 12 }, (_, index) => ({
  value: String(index + 1),
  label: `${index + 1} 月`,
}));
const draft = reactive(createDraftState());

const cronDescription = computed(() => describeCron(props.modelValue));
const previewCron = computed(() => {
  if (draft.mode === "custom") {
    return normalizeExpression(props.modelValue);
  }
  return buildCronExpression(draft, customCron.value);
});
const previewDescription = computed(() => describeCron(previewCron.value));

function createDraftState() {
  return {
    mode: "daily",
    minute: 0,
    hour: 8,
    day: 1,
    month: "1",
    weekday: "1",
    minuteInterval: 5,
    hourInterval: 6,
  };
}

function clampNumber(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return fallback;
  }
  return Math.max(min, Math.min(max, Math.floor(number)));
}

function pad(value) {
  return String(clampNumber(value, 0, 59, 0)).padStart(2, "0");
}

function normalizeExpression(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

function parseCronExpression(value) {
  const expression = normalizeExpression(value);
  const parsed = createDraftState();

  if (!expression) {
    return { parsed, custom: "" };
  }

  if (/^\* \* \* \* \*$/.test(expression)) {
    parsed.mode = "every_minute";
    return { parsed, custom: expression };
  }

  let match = expression.match(/^\*\/(\d{1,2}) \* \* \* \*$/);
  if (match) {
    parsed.mode = "every_minutes";
    parsed.minuteInterval = clampNumber(match[1], 1, 59, 5);
    return { parsed, custom: expression };
  }

  match = expression.match(/^(\d{1,2}) \* \* \* \*$/);
  if (match) {
    parsed.mode = "hourly";
    parsed.minute = clampNumber(match[1], 0, 59, 0);
    return { parsed, custom: expression };
  }

  match = expression.match(/^(\d{1,2}) \*\/(\d{1,2}) \* \* \*$/);
  if (match) {
    parsed.mode = "every_hours";
    parsed.minute = clampNumber(match[1], 0, 59, 0);
    parsed.hourInterval = clampNumber(match[2], 1, 23, 6);
    return { parsed, custom: expression };
  }

  match = expression.match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/);
  if (match) {
    parsed.mode = "daily";
    parsed.minute = clampNumber(match[1], 0, 59, 0);
    parsed.hour = clampNumber(match[2], 0, 23, 8);
    return { parsed, custom: expression };
  }

  match = expression.match(/^(\d{1,2}) (\d{1,2}) \* \* ([0-7])$/);
  if (match) {
    parsed.mode = "weekly";
    parsed.minute = clampNumber(match[1], 0, 59, 0);
    parsed.hour = clampNumber(match[2], 0, 23, 8);
    parsed.weekday = match[3] === "7" ? "0" : match[3];
    return { parsed, custom: expression };
  }

  match = expression.match(/^(\d{1,2}) (\d{1,2}) ([1-9]|[12]\d|3[01]) \* \*$/);
  if (match) {
    parsed.mode = "monthly";
    parsed.minute = clampNumber(match[1], 0, 59, 0);
    parsed.hour = clampNumber(match[2], 0, 23, 8);
    parsed.day = clampNumber(match[3], 1, 31, 1);
    return { parsed, custom: expression };
  }

  match = expression.match(/^(\d{1,2}) (\d{1,2}) ([1-9]|[12]\d|3[01]) ([1-9]|1[0-2]) \*$/);
  if (match) {
    parsed.mode = "yearly";
    parsed.minute = clampNumber(match[1], 0, 59, 0);
    parsed.hour = clampNumber(match[2], 0, 23, 8);
    parsed.day = clampNumber(match[3], 1, 31, 1);
    parsed.month = String(clampNumber(match[4], 1, 12, 1));
    return { parsed, custom: expression };
  }

  parsed.mode = "custom";
  return { parsed, custom: expression };
}

function buildCronExpression(state, rawValue = "") {
  const minute = clampNumber(state.minute, 0, 59, 0);
  const hour = clampNumber(state.hour, 0, 23, 8);
  const day = clampNumber(state.day, 1, 31, 1);
  const month = clampNumber(state.month, 1, 12, 1);
  const weekday = clampNumber(state.weekday, 0, 6, 1);
  const minuteInterval = clampNumber(state.minuteInterval, 1, 59, 5);
  const hourInterval = clampNumber(state.hourInterval, 1, 23, 6);

  switch (state.mode) {
    case "every_minute":
      return "* * * * *";
    case "every_minutes":
      return `*/${minuteInterval} * * * *`;
    case "hourly":
      return `${minute} * * * *`;
    case "every_hours":
      return `${minute} */${hourInterval} * * *`;
    case "daily":
      return `${minute} ${hour} * * *`;
    case "weekly":
      return `${minute} ${hour} * * ${weekday}`;
    case "monthly":
      return `${minute} ${hour} ${day} * *`;
    case "yearly":
      return `${minute} ${hour} ${day} ${month} *`;
    default:
      return normalizeExpression(rawValue);
  }
}

function describeCron(value) {
  const expression = normalizeExpression(value);
  if (!expression) {
    return "当前未设置 Cron。";
  }

  const matchers = [
    {
      regex: /^\* \* \* \* \*$/,
      format: () => "每分钟执行一次",
    },
    {
      regex: /^\*\/(\d{1,2}) \* \* \* \*$/,
      format: (match) => `每隔 ${match[1]} 分钟执行一次`,
    },
    {
      regex: /^(\d{1,2}) \* \* \* \*$/,
      format: (match) => `每小时的 ${pad(match[1])} 分执行`,
    },
    {
      regex: /^(\d{1,2}) \*\/(\d{1,2}) \* \* \*$/,
      format: (match) => `每隔 ${match[2]} 小时，在 ${pad(match[1])} 分执行`,
    },
    {
      regex: /^(\d{1,2}) (\d{1,2}) \* \* \*$/,
      format: (match) => `每天 ${pad(match[2])}:${pad(match[1])} 执行`,
    },
    {
      regex: /^(\d{1,2}) (\d{1,2}) \* \* ([0-7])$/,
      format: (match) => `${weekdayLabel(match[3])} ${pad(match[2])}:${pad(match[1])} 执行`,
    },
    {
      regex: /^(\d{1,2}) (\d{1,2}) ([1-9]|[12]\d|3[01]) \* \*$/,
      format: (match) => `每月 ${match[3]} 日 ${pad(match[2])}:${pad(match[1])} 执行`,
    },
    {
      regex: /^(\d{1,2}) (\d{1,2}) ([1-9]|[12]\d|3[01]) ([1-9]|1[0-2]) \*$/,
      format: (match) => `每年 ${match[4]} 月 ${match[3]} 日 ${pad(match[2])}:${pad(match[1])} 执行`,
    },
  ];

  for (const item of matchers) {
    const matched = expression.match(item.regex);
    if (matched) {
      return item.format(matched);
    }
  }

  return `自定义 Cron：${expression}`;
}

function weekdayLabel(value) {
  const mapping = {
    0: "每周日",
    1: "每周一",
    2: "每周二",
    3: "每周三",
    4: "每周四",
    5: "每周五",
    6: "每周六",
    7: "每周日",
  };
  return mapping[String(value)] || "每周";
}

function syncDraftFromValue(value) {
  const { parsed, custom } = parseCronExpression(value);
  draft.mode = parsed.mode;
  draft.minute = parsed.minute;
  draft.hour = parsed.hour;
  draft.day = parsed.day;
  draft.month = parsed.month;
  draft.weekday = parsed.weekday;
  draft.minuteInterval = parsed.minuteInterval;
  draft.hourInterval = parsed.hourInterval;
  customCron.value = custom || normalizeExpression(value);
}

function emitCron(value) {
  emit("update:modelValue", value);
}

function handleTextInput(event) {
  emitCron(event.target.value);
}

function openPopover() {
  popoverVisible.value = true;
}

function closePopover() {
  popoverVisible.value = false;
}

function selectMode(value) {
  const previousMode = draft.mode;
  if (value === "custom" && previousMode !== "custom") {
    customCron.value = buildCronExpression({ ...draft, mode: previousMode }, customCron.value);
  }
  draft.mode = value;
  if (value !== "custom") {
    applyPresetCron();
  }
}

function applyPresetCron() {
  if (draft.mode === "custom") {
    return;
  }
  const expression = buildCronExpression(draft, customCron.value);
  customCron.value = expression;
  emitCron(expression);
}

function handleDocumentPointerDown(event) {
  if (!popoverVisible.value) {
    return;
  }
  const root = rootRef.value;
  if (root && event.target instanceof Node && !root.contains(event.target)) {
    closePopover();
  }
}

function handleDocumentKeydown(event) {
  if (event.key === "Escape") {
    closePopover();
  }
}

watch(
  () => props.modelValue,
  (value) => {
    syncDraftFromValue(value);
  },
  { immediate: true },
);

onMounted(() => {
  document.addEventListener("pointerdown", handleDocumentPointerDown);
  document.addEventListener("keydown", handleDocumentKeydown);
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", handleDocumentPointerDown);
  document.removeEventListener("keydown", handleDocumentKeydown);
});
</script>
