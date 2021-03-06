# -*- coding: utf-8 -*-

import os

from flask import (Blueprint, render_template, request, url_for, redirect, g,
                   current_app, flash, abort)
from flask_babel import gettext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound

from db import db
from models import Journalist, InvalidUsernameException, PasswordError
from journalist_app.decorators import admin_required
from journalist_app.utils import (make_password, commit_account_changes,
                                  set_diceware_password)
from journalist_app.forms import LogoForm, NewUserForm


def make_blueprint(config):
    view = Blueprint('admin', __name__)

    @view.route('/', methods=('GET', 'POST'))
    @admin_required
    def index():
        users = Journalist.query.all()
        return render_template("admin.html", users=users)

    @view.route('/config', methods=('GET', 'POST'))
    @admin_required
    def manage_config():
        form = LogoForm()
        if form.validate_on_submit():
            f = form.logo.data
            static_filepath = os.path.join(config.SECUREDROP_ROOT,
                                           "static/i/logo.png")
            f.save(static_filepath)
            flash(gettext("Image updated."), "logo-success")
            return redirect(url_for("admin.manage_config"))
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    flash(error, "logo-error")
            return render_template("config.html", form=form)

    @view.route('/add', methods=('GET', 'POST'))
    @admin_required
    def add_user():
        form = NewUserForm()
        if form.validate_on_submit():
            form_valid = True
            username = request.form['username']
            password = request.form['password']
            is_admin = bool(request.form.get('is_admin'))

            try:
                otp_secret = None
                if request.form.get('is_hotp', False):
                    otp_secret = request.form.get('otp_secret', '')
                new_user = Journalist(username=username,
                                      password=password,
                                      is_admin=is_admin,
                                      otp_secret=otp_secret)
                db.session.add(new_user)
                db.session.commit()
            except PasswordError:
                flash(gettext(
                      'There was an error with the autogenerated password. '
                      'User not created. Please try again.'), 'error')
                form_valid = False
            except InvalidUsernameException as e:
                form_valid = False
                flash('Invalid username: ' + str(e), "error")
            except IntegrityError as e:
                db.session.rollback()
                form_valid = False
                if "UNIQUE constraint failed: journalists.username" in str(e):
                    flash(gettext("That username is already in use"),
                          "error")
                else:
                    flash(gettext("An error occurred saving this user"
                                  " to the database."
                                  " Please inform your administrator."),
                          "error")
                    current_app.logger.error("Adding user "
                                             "'{}' failed: {}".format(
                                                 username, e))

            if form_valid:
                return redirect(url_for('admin.new_user_two_factor',
                                        uid=new_user.id))

        return render_template("admin_add_user.html",
                               password=make_password(config),
                               form=form)

    @view.route('/2fa', methods=('GET', 'POST'))
    @admin_required
    def new_user_two_factor():
        user = Journalist.query.get(request.args['uid'])

        if request.method == 'POST':
            token = request.form['token']
            if user.verify_token(token):
                flash(gettext(
                    "Token in two-factor authentication "
                    "accepted for user {user}.").format(
                        user=user.username),
                    "notification")
                return redirect(url_for("admin.index"))
            else:
                flash(gettext(
                    "Could not verify token in two-factor authentication."),
                      "error")

        return render_template("admin_new_user_two_factor.html", user=user)

    @view.route('/reset-2fa-totp', methods=['POST'])
    @admin_required
    def reset_two_factor_totp():
        uid = request.form['uid']
        user = Journalist.query.get(uid)
        user.is_totp = True
        user.regenerate_totp_shared_secret()
        db.session.commit()
        return redirect(url_for('admin.new_user_two_factor', uid=uid))

    @view.route('/reset-2fa-hotp', methods=['POST'])
    @admin_required
    def reset_two_factor_hotp():
        uid = request.form['uid']
        otp_secret = request.form.get('otp_secret', None)
        if otp_secret:
            user = Journalist.query.get(uid)
            try:
                user.set_hotp_secret(otp_secret)
            except TypeError as e:
                if "Non-hexadecimal digit found" in str(e):
                    flash(gettext(
                        "Invalid secret format: "
                        "please only submit letters A-F and numbers 0-9."),
                          "error")
                elif "Odd-length string" in str(e):
                    flash(gettext(
                        "Invalid secret format: "
                        "odd-length secret. Did you mistype the secret?"),
                          "error")
                else:
                    flash(gettext(
                        "An unexpected error occurred! "
                        "Please inform your administrator."), "error")
                    current_app.logger.error(
                        "set_hotp_secret '{}' (id {}) failed: {}".format(
                            otp_secret, uid, e))
                return render_template('admin_edit_hotp_secret.html', uid=uid)
            else:
                db.session.commit()
                return redirect(url_for('admin.new_user_two_factor', uid=uid))
        else:
            return render_template('admin_edit_hotp_secret.html', uid=uid)

    @view.route('/edit/<int:user_id>', methods=('GET', 'POST'))
    @admin_required
    def edit_user(user_id):
        user = Journalist.query.get(user_id)

        if request.method == 'POST':
            if request.form.get('username', None):
                new_username = request.form['username']

                try:
                    Journalist.check_username_acceptable(new_username)
                except InvalidUsernameException as e:
                    flash('Invalid username: ' + str(e), 'error')
                    return redirect(url_for("admin.edit_user",
                                            user_id=user_id))

                if new_username == user.username:
                    pass
                elif Journalist.query.filter_by(
                        username=new_username).one_or_none():
                    flash(gettext(
                        'Username "{user}" already taken.').format(
                            user=new_username),
                        "error")
                    return redirect(url_for("admin.edit_user",
                                            user_id=user_id))
                else:
                    user.username = new_username

            user.is_admin = bool(request.form.get('is_admin'))

            commit_account_changes(user)

        password = make_password(config)
        return render_template("edit_account.html", user=user,
                               password=password)

    @view.route('/edit/<int:user_id>/new-password', methods=('POST',))
    @admin_required
    def set_password(user_id):
        try:
            user = Journalist.query.get(user_id)
        except NoResultFound:
            abort(404)

        password = request.form.get('password')
        set_diceware_password(user, password)
        return redirect(url_for('admin.edit_user', user_id=user_id))

    @view.route('/delete/<int:user_id>', methods=('POST',))
    @admin_required
    def delete_user(user_id):
        user = Journalist.query.get(user_id)
        if user_id == g.user.id:
            # Do not flash because the interface already has safe guards.
            # It can only happen by manually crafting a POST request
            current_app.logger.error(
                "Admin {} tried to delete itself".format(g.user.username))
            abort(403)
        elif user:
            db.session.delete(user)
            db.session.commit()
            flash(gettext("Deleted user '{user}'").format(
                user=user.username), "notification")
        else:
            current_app.logger.error(
                "Admin {} tried to delete nonexistent user with pk={}".format(
                    g.user.username, user_id))
            abort(404)

        return redirect(url_for('admin.index'))

    @view.route('/edit/<int:user_id>/new-password', methods=('POST',))
    @admin_required
    def new_password(user_id):
        try:
            user = Journalist.query.get(user_id)
        except NoResultFound:
            abort(404)

        password = request.form.get('password')
        set_diceware_password(user, password)
        return redirect(url_for('admin.edit_user', user_id=user_id))

    @view.route('/ossec-test')
    @admin_required
    def ossec_test():
        current_app.logger.error('This is a test OSSEC alert')
        flash(gettext('Test alert sent. Check your email.'), 'notification')
        return redirect(url_for('admin.manage_config'))

    return view
