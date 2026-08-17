// zatca_api/zatca_api/doctype/zatca_api_settings/zatca_api_settings.js
// Copyright (c) 2026, Enfono Technologies and contributors

frappe.ui.form.on('ZATCA API Settings', {
	refresh(frm) {
		frm.add_custom_button(__('Run Readiness Check'), () => run_ping(frm));

		frm.add_custom_button(__('Open User Guide'), () => {
			window.open('/user-guide', '_blank');
		});

		frm.add_custom_button(__('View Request Log'), () => {
			frappe.set_route('List', 'ZATCA API Request Log');
		});

		if (frm.doc.pull_enabled && (frm.doc.sources || []).length) {
			frm.add_custom_button(
				__('Pull Now'),
				() => run_pull(frm),
				__('Pull Sources')
			);
		}

		render_banner(frm);
	},

	auto_submit_invoices(frm) {
		if (!frm.doc.auto_submit_invoices) {
			frappe.msgprint({
				title: __('No QR Will Be Returned'),
				indicator: 'orange',
				// A ZATCA QR only exists for a submitted invoice, so turning this off
				// silently changes what the API can return.
				message: __(
					'Invoices will be left in Draft. A ZATCA QR only exists for a submitted invoice, so API responses will report zatca.available = false until each invoice is submitted.'
				),
			});
		}
	},

	wait_for_zatca_seconds(frm) {
		if (cint(frm.doc.wait_for_zatca_seconds) > 0) {
			frappe.msgprint({
				title: __('Not Recommended'),
				indicator: 'orange',
				message: __(
					'The QR, UUID and invoice hash are computed locally and are already in the create response. Only the clearance status is asynchronous. Holding the HTTP request open ties up a web worker — poll get_status instead.'
				),
			});
		}
	},

	allow_amend_submitted(frm) {
		if (frm.doc.allow_amend_submitted) {
			frappe.msgprint({
				title: __('Dangerous'),
				indicator: 'red',
				message: __(
					'Modifying a submitted invoice rewrites its GL entries, and once ZATCA has cleared an invoice it is legally immutable. Leave this off unless you fully understand the consequence.'
				),
			});
		}
	},
});

function render_banner(frm) {
	if (!frm.doc.enabled) {
		frm.dashboard.clear_headline();
		frm.dashboard.set_headline(
			`<span class="indicator red">${__('ZATCA API is disabled — every endpoint returns HTTP 503.')}</span>`
		);
		return;
	}

	const mode = frm.doc.submit_mode === 'Queued' ? __('Queued') : __('Immediate');
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline(
		`<span class="indicator green">${__('Enabled')}</span> &nbsp; ` +
			`${__('Submit mode')}: <b>${mode}</b> &nbsp;&middot;&nbsp; ` +
			`${__('Phase')}: <b>${frappe.utils.escape_html(frm.doc.zatca_phase || 'Auto')}</b>`
	);
}

function run_ping(frm) {
	frappe.call({
		method: 'zatca_api.api.v1.ping',
		// ping is declared @frappe.whitelist(methods=['GET']). frappe.call POSTs by
		// default, and frappe's is_valid_http_method rejects a verb the endpoint does not
		// declare with a bare "Not permitted" PermissionError -- which reads like a
		// missing role rather than a wrong verb. Keep this in step with the decorator.
		type: 'GET',
		freeze: true,
		freeze_message: __('Checking readiness...'),
		callback: (r) => {
			const env = r.message || {};
			if (!env.success) {
				const err = (env.errors || [{}])[0];
				frappe.msgprint({
					title: __('Not Ready'),
					indicator: 'red',
					message: frappe.utils.escape_html(err.message || __('Unknown error')),
				});
				return;
			}

			const d = env.data || {};
			const readiness = d.readiness || {};
			const rows = (readiness.companies || [])
				.map(
					(c) =>
						`<tr><td>${frappe.utils.escape_html(c.company)}</td>` +
						`<td>${frappe.utils.escape_html(c.phase)}</td></tr>`
				)
				.join('');

			const tick = (ok) =>
				ok
					? '<span class="indicator green">OK</span>'
					: '<span class="indicator red">Missing</span>';

			frappe.msgprint({
				title: __('Readiness'),
				indicator: 'green',
				message: `
					<table class="table table-bordered">
						<tr><td>${__('App version')}</td><td>${frappe.utils.escape_html(d.version || '')}</td></tr>
						<tr><td>${__('Invoice custom fields')}</td><td>${tick(d.custom_fields_installed)}</td></tr>
						<tr><td>${__('ksa_compliance installed')}</td><td>${tick(readiness.ksa_compliance_installed)}</td></tr>
						<tr><td>${__('ksa_compliance version')}</td><td>${frappe.utils.escape_html(readiness.ksa_compliance_version || '-')}</td></tr>
						<tr><td>${__('Submit mode')}</td><td>${frappe.utils.escape_html(d.submit_mode || '')}</td></tr>
					</table>
					${rows ? `<h5>${__('Companies')}</h5><table class="table table-bordered"><thead><tr><th>${__('Company')}</th><th>${__('Phase')}</th></tr></thead><tbody>${rows}</tbody></table>` : ''}
					<p class="text-muted">${__('If custom fields are missing, run: bench --site &lt;site&gt; migrate')}</p>
				`,
			});
		},
	});
}

function run_pull(frm) {
	frappe.prompt(
		[
			{
				fieldname: 'source',
				label: __('Source'),
				fieldtype: 'Select',
				options: (frm.doc.sources || []).map((s) => s.source_name).join('\n'),
				reqd: 1,
			},
		],
		(values) => {
			frappe.call({
				method: 'zatca_api.api.v1.pull_now',
				args: { source: values.source },
				freeze: true,
				freeze_message: __('Pulling invoices...'),
				callback: (r) => {
					const env = r.message || {};
					frappe.msgprint({
						title: env.success ? __('Pull Complete') : __('Pull Failed'),
						indicator: env.success ? 'green' : 'red',
						message: `<pre>${frappe.utils.escape_html(
							JSON.stringify(env.success ? env.data : env.errors, null, 2)
						)}</pre>`,
					});
				},
			});
		},
		__('Pull Now'),
		__('Run')
	);
}
