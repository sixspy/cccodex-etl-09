const { createApp } = Vue;

createApp({
  data() {
    return {
      me: "",
      items: [],
      current: null,
      detail: { sample: [], audits: [], claims: [] },
      sample: [],
      busy: false,
      msg: "",
      ov: {
        pending: 0,
        masked: 0,
        audits: 0,
        strength: "",
        strength_name: "未选定",
        strength_desc: "",
        day: "",
        extraction_open: false,
      },
      rules: [],
      options: { strengths: [], mask_types: [] },
      rule: { name: "", field: "手机号", mask_type: "phone" },
      ruleMsg: "",
      policyMsg: "",
    };
  },
  async mounted() {
    const me = await fetch("/api/me");
    if (!me.ok) {
      location.href = "/login";
      return;
    }
    this.me = (await me.json()).user;
    await Promise.all([this.loadOptions(), this.loadOverview(), this.loadRules()]);
    await this.refresh();
  },
  methods: {
    tagClass(status) {
      if (status === "已落库") return "ok";
      if (status === "已抽取待加载") return "warn";
      return "";
    },
    maskChipClass(status) {
      if (status === "已掩码") return "ok";
      if (status === "待脱敏") return "warn";
      if (status === "明文落库") return "warn";
      return "";
    },
    typeName(key) {
      const t = this.options.mask_types.find((x) => x.key === key);
      return t ? t.name : key;
    },
    async loadOptions() {
      const res = await fetch("/api/masking/options");
      if (res.ok) this.options = await res.json();
    },
    async loadOverview() {
      const res = await fetch("/api/masking/overview");
      if (!res.ok) return;
      this.ov = await res.json();
    },
    async loadRules() {
      const res = await fetch("/api/masking/rules");
      if (!res.ok) return;
      this.rules = (await res.json()).items || [];
    },
    async refresh() {
      const res = await fetch("/api/pipelines");
      if (!res.ok) return;
      this.items = (await res.json()).items || [];
      if (this.current) {
        const fresh = this.items.find((x) => x.id === this.current.id);
        if (fresh) this.current = fresh;
      }
    },
    async open(id) {
      const res = await fetch(`/api/pipelines/${id}`);
      if (!res.ok) return;
      this.current = await res.json();
      this.msg = "";
      await this.loadDetail();
    },
    async loadDetail() {
      if (!this.current) return;
      const res = await fetch(`/api/pipelines/${this.current.id}/mask`);
      if (!res.ok) return;
      this.detail = await res.json();
      this.sample = this.detail.sample || [];
    },
    async post(url, body) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      let data = {};
      try {
        data = await res.json();
      } catch (e) {
        data = {};
      }
      return { ok: res.ok, status: res.status, data };
    },
    async extract() {
      if (!this.current) return;
      this.busy = true;
      this.msg = "";
      try {
        const res = await this.post(`/api/pipelines/${this.current.id}/extract`);
        if (!res.ok) {
          this.msg = res.data.detail || res.data.error || "抽取失败";
          return;
        }
        this.msg = "抽取完成，可提交脱敏";
        await this.afterChange();
      } finally {
        this.busy = false;
      }
    },
    async startExtract() {
      if (!this.current) return;
      this.busy = true;
      try {
        const res = await this.post(`/api/pipelines/${this.current.id}/extract/start`);
        this.msg = res.ok ? "抽取已开始，收束前不可改脱敏规则" : "开启失败";
        await this.afterChange();
      } finally {
        this.busy = false;
      }
    },
    async finishExtract() {
      if (!this.current) return;
      this.busy = true;
      try {
        const res = await this.post(`/api/pipelines/${this.current.id}/extract/finish`);
        this.msg = res.ok ? "抽取已收束" : "收束失败";
        await this.afterChange();
      } finally {
        this.busy = false;
      }
    },
    async submitMask(claimer) {
      if (!this.current) return;
      this.busy = true;
      this.msg = "";
      try {
        const res = await this.post(`/api/pipelines/${this.current.id}/mask`, {
          claimer,
          strength: this.ov.strength || "",
        });
        if (!res.ok) {
          this.msg = res.data.error || res.data.detail || "脱敏提交失败";
          await this.afterChange();
          return;
        }
        this.msg = `脱敏完成：${res.data.audits} 条审计明细`;
        await this.afterChange();
      } finally {
        this.busy = false;
      }
    },
    async runLoad() {
      if (!this.current) return;
      this.busy = true;
      this.msg = "";
      try {
        const res = await this.post(`/api/pipelines/${this.current.id}/load`, {
          strength: this.ov.strength || "",
        });
        if (!res.ok) {
          this.msg = res.data.error || res.data.detail || "落库被拦截";
          return;
        }
        this.msg = res.data.status === "已落库" ? "落库完成" : "当前状态不能落库，请先抽取";
        await this.afterChange();
      } finally {
        this.busy = false;
      }
    },
    async setStrength(key) {
      this.busy = true;
      this.policyMsg = "";
      try {
        const res = await this.post("/api/masking/policy", { strength: key });
        if (!res.ok) {
          this.policyMsg = res.data.error || "强度设置失败";
          return;
        }
        this.policyMsg = `已写入当天策略：${res.data.strength_name}（当天不可更换）`;
        await this.loadOverview();
      } finally {
        this.busy = false;
      }
    },
    async saveRule() {
      this.busy = true;
      this.ruleMsg = "";
      try {
        const res = await fetch("/api/masking/rules", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.rule),
        });
        const data = await res.json();
        if (!res.ok) {
          this.ruleMsg = data.error || data.detail || "规则保存失败";
          return;
        }
        this.ruleMsg = "规则已保存";
        this.rule = { name: "", field: "手机号", mask_type: "phone" };
        await this.loadRules();
        await this.loadOverview();
        if (this.current) await this.loadDetail();
      } finally {
        this.busy = false;
      }
    },
    async removeRule(id) {
      this.busy = true;
      this.ruleMsg = "";
      try {
        const res = await fetch(`/api/masking/rules/${id}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) {
          this.ruleMsg = data.error || data.detail || "删除失败";
          return;
        }
        await this.loadRules();
      } finally {
        this.busy = false;
      }
    },
    async afterChange() {
      await Promise.all([this.refresh(), this.loadOverview()]);
      await this.loadDetail();
    },
    async logout() {
      await fetch("/api/logout", { method: "POST" });
      location.href = "/login";
    },
  },
}).mount("#app");
