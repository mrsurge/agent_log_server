let bootConfig = { schemas: {} };

function normalizeRequestMethod(value) {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function decisionLabel(decision, helpers) {
  if (helpers?.normalizeDecisionLabel) {
    return helpers.normalizeDecisionLabel(decision);
  }
  if (typeof decision === 'string') return decision;
  if (decision && typeof decision === 'object') return 'Submit';
  return 'Submit';
}

function collectStringDecisionOptions(schemaNode, result = []) {
  if (!schemaNode || typeof schemaNode !== 'object') return result;
  if (Array.isArray(schemaNode.enum)) {
    schemaNode.enum.forEach((item) => {
      if (typeof item === 'string' && !result.includes(item)) {
        result.push(item);
      }
    });
  }
  if (typeof schemaNode.const === 'string' && !result.includes(schemaNode.const)) {
    result.push(schemaNode.const);
  }
  ['oneOf', 'anyOf', 'allOf'].forEach((key) => {
    const variants = schemaNode[key];
    if (Array.isArray(variants)) {
      variants.forEach((variant) => collectStringDecisionOptions(variant, result));
    }
  });
  if (schemaNode.properties && typeof schemaNode.properties === 'object') {
    Object.values(schemaNode.properties).forEach((value) => collectStringDecisionOptions(value, result));
  }
  return result;
}

function responseDecisionOptions(schema, requestParams) {
  const available = Array.isArray(requestParams?.availableDecisions) ? requestParams.availableDecisions : [];
  if (available.length) {
    return available;
  }
  const responseSchema = schema?.response;
  const decisionSchema = responseSchema?.properties?.decision;
  const options = collectStringDecisionOptions(decisionSchema, []);
  if (options.length) {
    return options;
  }
  return ['accept', 'decline'];
}

function appendKeyValue(container, label, value, helpers) {
  if (value === null || value === undefined || value === '') return;
  const row = document.createElement('div');
  row.innerHTML = `<strong>${helpers.escapeHtml(label)}:</strong> ${helpers.escapeHtml(String(value))}`;
  container.append(row);
}

function createFeedbackNode(body) {
  const feedback = document.createElement('div');
  feedback.className = 'approval-feedback';
  body.append(feedback);
  return feedback;
}

function setFeedback(node, message, isError = false) {
  if (!(node instanceof HTMLElement)) return;
  node.textContent = message || '';
  node.style.color = isError ? '#c62828' : '';
}

function isReadOnlyEvent(event, helpers) {
  return helpers?.readOnly === true
    || event?.replay === true
    || event?.event === 'approval_decision'
    || typeof event?.status === 'string';
}

function readOnlyStatusLabel(event, fallback = 'Recorded response') {
  const parts = [];
  if (typeof event?.status === 'string' && event.status.trim()) parts.push(event.status.trim());
  if (typeof event?.decision === 'string' && event.decision.trim()) parts.push(event.decision.trim());
  if (typeof event?.result?.action === 'string' && event.result.action.trim()) parts.push(event.result.action.trim());
  return parts.length ? `${fallback}: ${parts.join(' / ')}` : fallback;
}

function readOnlyQuestionAnswers(result, questionId) {
  const answerEntry = result?.answers?.[questionId];
  if (!answerEntry) return [];
  if (Array.isArray(answerEntry?.answers)) {
    return answerEntry.answers.map((item) => String(item || '')).filter(Boolean);
  }
  if (typeof answerEntry?.answer === 'string' && answerEntry.answer.trim()) {
    return [answerEntry.answer.trim()];
  }
  if (typeof answerEntry === 'string' && answerEntry.trim()) {
    return [answerEntry.trim()];
  }
  return [];
}

async function trySubmit(helpers, result, meta, feedbackNode, pendingMessage = '') {
  if (pendingMessage) {
    setFeedback(feedbackNode, pendingMessage, false);
  }
  const outcome = await helpers.submitResult(result, meta);
  if (!outcome || outcome.ok === false) {
    const message = outcome?.response?.error || 'Request failed';
    setFeedback(feedbackNode, message, true);
    return false;
  }
  return true;
}

function addJsonDetails(body, label, value) {
  if (value === null || value === undefined || value === '') return;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = label;
  const pre = document.createElement('pre');
  pre.className = 'approval-extra';
  pre.textContent = JSON.stringify(value, null, 2);
  details.append(summary, pre);
  body.append(details);
}

function createOptionDescription(text) {
  const description = document.createElement('div');
  description.className = 'approval-option-description';
  description.textContent = String(text || '');
  return description;
}

function createSubmittedAnswerNode(values) {
  const wrapper = document.createElement('div');
  wrapper.className = 'approval-summary';
  const title = document.createElement('div');
  title.innerHTML = '<strong>Response:</strong>';
  wrapper.append(title);
  values.forEach((value) => {
    const row = document.createElement('div');
    row.textContent = String(value || '');
    wrapper.append(row);
  });
  return wrapper;
}

function splitAnswers(raw) {
  return String(raw || '')
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseJsonLike(text, fallback = null) {
  const raw = String(text || '').trim();
  if (!raw) return fallback;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function renderCommandCard(body, event, schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const readOnly = isReadOnlyEvent(event, helpers);
  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendKeyValue(summary, 'Command', Array.isArray(payload.command) ? payload.command.join(' ') : payload.command || requestParams.command || '', helpers);
  appendKeyValue(summary, 'CWD', payload.cwd || requestParams.cwd || '', helpers);
  appendKeyValue(summary, 'Reason', payload.reason || requestParams.reason || '', helpers);
  body.append(summary);

  addJsonDetails(body, 'Command request details', {
    availableDecisions: requestParams.availableDecisions || null,
    commandActions: payload.command_actions || requestParams.commandActions || null,
    additionalPermissions: payload.additional_permissions || requestParams.additionalPermissions || null,
    proposedExecpolicyAmendment: payload.proposed_execpolicy_amendment || requestParams.proposedExecpolicyAmendment || null,
    proposedNetworkPolicyAmendments: payload.proposed_network_policy_amendments || requestParams.proposedNetworkPolicyAmendments || null,
    networkApprovalContext: payload.network_approval_context || requestParams.networkApprovalContext || null,
  });

  if (readOnly) {
    const feedback = createFeedbackNode(body);
    feedback.classList.add('approval-feedback-static');
    setFeedback(feedback, readOnlyStatusLabel(event), false);
    if (event?.result && typeof event.result === 'object') {
      addJsonDetails(body, 'Recorded result', event.result);
    }
    return;
  }

  const feedback = createFeedbackNode(body);
  const actions = document.createElement('div');
  actions.className = 'actions';
  responseDecisionOptions(schema, requestParams).forEach((decision) => {
    const button = document.createElement('button');
    button.className = 'btn tiny approve';
    button.textContent = decisionLabel(decision, helpers);
    button.addEventListener('click', async () => {
      await trySubmit(helpers, { decision }, {}, feedback, 'Sending response…');
    });
    actions.append(button);
  });
  body.append(actions);
}

function renderFileChangeCard(body, event, schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const readOnly = isReadOnlyEvent(event, helpers);
  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendKeyValue(summary, 'Reason', payload.reason || requestParams.reason || '', helpers);
  appendKeyValue(summary, 'Grant root', payload.grant_root || requestParams.grantRoot || '', helpers);
  body.append(summary);

  const diffText = payload.diff || '';
  const filePath = payload.path || null;
  if (diffText) {
    const diffBlock = document.createElement('pre');
    diffBlock.className = 'diff-block';
    diffBlock.innerHTML = helpers.formatDiff(diffText, filePath);
    body.append(diffBlock);
  } else if (payload.changes) {
    addJsonDetails(body, 'File change details', payload.changes);
  }

  if (readOnly) {
    const feedback = createFeedbackNode(body);
    feedback.classList.add('approval-feedback-static');
    setFeedback(feedback, readOnlyStatusLabel(event), false);
    if (event?.result && typeof event.result === 'object') {
      addJsonDetails(body, 'Recorded result', event.result);
    }
    return;
  }

  const feedback = createFeedbackNode(body);
  const actions = document.createElement('div');
  actions.className = 'actions';
  responseDecisionOptions(schema, requestParams).forEach((decision) => {
    const button = document.createElement('button');
    button.className = decision === 'decline' || decision === 'cancel' ? 'btn tiny decline' : 'btn tiny approve';
    button.textContent = decisionLabel(decision, helpers);
    button.addEventListener('click', async () => {
      await trySubmit(helpers, { decision }, { diff: diffText, path: filePath }, feedback, 'Sending response…');
    });
    actions.append(button);
  });
  body.append(actions);
}

function renderUserInputCard(body, event, _schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const questions = Array.isArray(requestParams.questions) ? requestParams.questions : (Array.isArray(payload.questions) ? payload.questions : []);
  const readOnly = isReadOnlyEvent(event, helpers);
  const result = event?.result && typeof event.result === 'object' ? event.result : {};
  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendKeyValue(summary, 'Request', payload.message || 'Tool is waiting for user input', helpers);
  body.append(summary);

  const buildAnswers = (overrides = {}) => {
    const answers = {};
    body.querySelectorAll('[data-question-id]').forEach((inputEl) => {
      const questionId = inputEl.getAttribute('data-question-id') || '';
      if (!questionId) return;
      const override = overrides[questionId];
      const raw = override !== undefined ? override : ('value' in inputEl ? inputEl.value : '');
      const values = Array.isArray(raw) ? raw : splitAnswers(raw);
      if (values.length) {
        answers[questionId] = { answers: values };
      }
    });
    return answers;
  };

  const rerenderSubmitted = (answers, statusLabel = 'submitted') => {
    renderUserInputCard(
      body,
      {
        ...event,
        status: statusLabel,
        result: { answers },
      },
      _schema,
      {
        ...helpers,
        readOnly: true,
      },
    );
  };

  questions.forEach((question) => {
    if (!question || typeof question !== 'object') return;
    const wrapper = document.createElement('div');
    wrapper.className = 'approval-question';

    const title = document.createElement('div');
    title.innerHTML = `<strong>${helpers.escapeHtml(String(question.header || question.id || 'Question'))}</strong>`;
    wrapper.append(title);

    if (question.question) {
      const prompt = document.createElement('div');
      prompt.textContent = String(question.question);
      wrapper.append(prompt);
    }

    const questionId = String(question.id || '');
    const recordedAnswers = readOnlyQuestionAnswers(result, questionId);

    if (Array.isArray(question.options) && question.options.length) {
      const optionsWrap = document.createElement('div');
      optionsWrap.className = 'approval-option-list';
      question.options.forEach((option) => {
        if (!option || typeof option !== 'object') return;
        const item = document.createElement('div');
        item.className = 'approval-option-item';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn tiny';
        const optionLabel = String(option.label || '');
        button.textContent = optionLabel;
        if (recordedAnswers.includes(optionLabel)) {
          button.classList.add('approve');
        }
        if (readOnly) {
          button.disabled = true;
        } else {
          button.addEventListener('click', async () => {
            const answers = buildAnswers({ [questionId]: [optionLabel] });
            const ok = await trySubmit(helpers, { answers }, {}, feedback, 'Sending answers…');
            if (ok) {
              rerenderSubmitted(answers);
            }
          });
        }
        item.append(button);
        if (option.description) {
          item.append(createOptionDescription(option.description));
        }
        optionsWrap.append(item);
      });
      wrapper.append(optionsWrap);
    }

    if (readOnly) {
      if (recordedAnswers.length) {
        wrapper.append(createSubmittedAnswerNode(recordedAnswers));
      }
      body.append(wrapper);
      return;
    }

    const freeformLabel = document.createElement('div');
    freeformLabel.innerHTML = '<strong>Something else:</strong>';
    wrapper.append(freeformLabel);

    const field = question.isSecret ? document.createElement('input') : document.createElement('textarea');
    field.setAttribute('data-question-id', questionId);
    if (field instanceof HTMLInputElement) {
      field.type = 'password';
      field.className = 'input secret approval-answer-field';
      field.placeholder = question.isOther ? 'Enter response' : 'Response';
    } else {
      field.className = 'input approval-answer-field';
      field.rows = 2;
      field.placeholder = question.isOther ? 'Enter one or more responses (one per line)' : 'Enter response';
    }
    wrapper.append(field);

    body.append(wrapper);
  });

  const feedback = createFeedbackNode(body);
  if (readOnly) {
    feedback.classList.add('approval-feedback-static');
    setFeedback(feedback, readOnlyStatusLabel(event), false);
    return;
  }
  const actions = document.createElement('div');
  actions.className = 'actions';
  const sendButton = document.createElement('button');
  sendButton.className = 'btn tiny approve';
  sendButton.textContent = 'Send';
  sendButton.addEventListener('click', async () => {
    const answers = buildAnswers();
    if (!Object.keys(answers).length) {
      setFeedback(feedback, 'Enter at least one answer first.', true);
      return;
    }
    const ok = await trySubmit(helpers, { answers }, {}, feedback, 'Sending answers…');
    if (ok) {
      rerenderSubmitted(answers);
    }
  });
  actions.append(sendButton);
  body.append(actions);
}

function renderToolCallCard(body, event, _schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const readOnly = isReadOnlyEvent(event, helpers);
  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendKeyValue(summary, 'Tool', payload.tool || requestParams.tool || 'tool', helpers);
  appendKeyValue(summary, 'Call ID', payload.call_id || requestParams.callId || '', helpers);
  body.append(summary);
  addJsonDetails(body, 'Tool arguments', payload.arguments ?? requestParams.arguments ?? null);

  const textarea = document.createElement('textarea');
  textarea.className = 'input';
  textarea.rows = 4;
  textarea.placeholder = 'Enter tool output text';
  if (readOnly && Array.isArray(event?.result?.contentItems)) {
    textarea.value = event.result.contentItems
      .map((item) => (item && typeof item.text === 'string') ? item.text : '')
      .filter(Boolean)
      .join('\n');
    textarea.readOnly = true;
  }
  body.append(textarea);

  const feedback = createFeedbackNode(body);
  if (readOnly) {
    feedback.classList.add('approval-feedback-static');
    setFeedback(feedback, readOnlyStatusLabel(event), false);
    if (event?.result && typeof event.result === 'object') {
      addJsonDetails(body, 'Recorded result', event.result);
    }
    return;
  }
  const actions = document.createElement('div');
  actions.className = 'actions';

  const sendButton = document.createElement('button');
  sendButton.className = 'btn tiny approve';
  sendButton.textContent = 'Send';
  sendButton.addEventListener('click', async () => {
    const result = {
      contentItems: textarea.value.trim() ? [{ type: 'inputText', text: textarea.value }] : [],
      success: true,
    };
    await trySubmit(helpers, result, {}, feedback, 'Sending tool result…');
  });
  actions.append(sendButton);

  const failButton = document.createElement('button');
  failButton.className = 'btn tiny decline';
  failButton.textContent = 'Fail';
  failButton.addEventListener('click', async () => {
    const result = {
      contentItems: textarea.value.trim() ? [{ type: 'inputText', text: textarea.value }] : [],
      success: false,
    };
    await trySubmit(helpers, result, {}, feedback, 'Sending tool result…');
  });
  actions.append(failButton);
  body.append(actions);
}

function renderElicitationCard(body, event, _schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const readOnly = isReadOnlyEvent(event, helpers);
  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendKeyValue(summary, 'Server', payload.server_name || requestParams.serverName || '', helpers);
  appendKeyValue(summary, 'Mode', payload.mode || requestParams.mode || '', helpers);
  appendKeyValue(summary, 'Message', payload.message || requestParams.message || '', helpers);
  body.append(summary);

  if (payload.url || requestParams.url) {
    const link = document.createElement('a');
    link.href = String(payload.url || requestParams.url);
    link.target = '_blank';
    link.rel = 'noreferrer';
    link.textContent = 'Open elicitation URL';
    body.append(link);
  }

  addJsonDetails(body, 'Requested schema', payload.requested_schema ?? requestParams.requestedSchema ?? null);

  const textarea = document.createElement('textarea');
  textarea.className = 'input';
  textarea.rows = 5;
  textarea.placeholder = 'Enter JSON or plain text response';
  if (readOnly && event?.result && typeof event.result === 'object') {
    textarea.value = JSON.stringify(event.result.content ?? event.result, null, 2);
    textarea.readOnly = true;
  }
  body.append(textarea);

  const feedback = createFeedbackNode(body);
  if (readOnly) {
    feedback.classList.add('approval-feedback-static');
    setFeedback(feedback, readOnlyStatusLabel(event), false);
    if (event?.result && typeof event.result === 'object') {
      addJsonDetails(body, 'Recorded result', event.result);
    }
    return;
  }
  const actions = document.createElement('div');
  actions.className = 'actions';

  const acceptButton = document.createElement('button');
  acceptButton.className = 'btn tiny approve';
  acceptButton.textContent = 'Accept';
  acceptButton.addEventListener('click', async () => {
    const mode = String(payload.mode || requestParams.mode || '').trim().toLowerCase();
    const defaultContent = mode === 'form' ? {} : null;
    const result = {
      action: 'accept',
      content: parseJsonLike(textarea.value, defaultContent),
    };
    await trySubmit(helpers, result, {}, feedback, 'Sending response…');
  });
  actions.append(acceptButton);

  const declineButton = document.createElement('button');
  declineButton.className = 'btn tiny decline';
  declineButton.textContent = 'Decline';
  declineButton.addEventListener('click', async () => {
    await trySubmit(helpers, { action: 'decline', content: null }, {}, feedback, 'Sending response…');
  });
  actions.append(declineButton);

  const cancelButton = document.createElement('button');
  cancelButton.className = 'btn tiny decline';
  cancelButton.textContent = 'Cancel';
  cancelButton.addEventListener('click', async () => {
    await trySubmit(helpers, { action: 'cancel', content: null }, {}, feedback, 'Sending response…');
  });
  actions.append(cancelButton);
  body.append(actions);
}

export function initializeRequestCardModule(config = {}) {
  bootConfig = {
    schemas: config.schemas && typeof config.schemas === 'object' ? config.schemas : {},
  };
}

export async function renderRequestCard(ctx = {}) {
  const event = ctx.event && typeof ctx.event === 'object' ? ctx.event : {};
  const helpers = ctx.helpers && typeof ctx.helpers === 'object' ? ctx.helpers : {};
  const body = ctx.body;
  if (!(body instanceof HTMLElement)) return false;
  const requestMethod = normalizeRequestMethod(event.request_method || event.requestMethod);
  const schema = ctx.schema || bootConfig.schemas?.[requestMethod] || null;

  if (requestMethod === 'item/commandexecution/requestapproval') {
    renderCommandCard(body, event, schema, helpers);
    return true;
  }
  if (requestMethod === 'item/filechange/requestapproval') {
    renderFileChangeCard(body, event, schema, helpers);
    return true;
  }
  if (requestMethod === 'item/tool/requestuserinput') {
    renderUserInputCard(body, event, schema, helpers);
    return true;
  }
  if (requestMethod === 'item/tool/call') {
    renderToolCallCard(body, event, schema, helpers);
    return true;
  }
  if (requestMethod === 'mcpserver/elicitation/request') {
    renderElicitationCard(body, event, schema, helpers);
    return true;
  }
  return false;
}
