import React, { PropTypes } from 'react';

import APINotLoaded from 'es6!display/not_loaded';
import ChangesLinks from 'es6!display/changes/links';
import { AjaxError } from 'es6!display/errors';
import { ChangesChart } from 'es6!display/changes/charts';
import { Grid } from 'es6!display/grid';
import { Menu1 } from 'es6!display/menus';
import { SingleBuildStatus, get_build_cause } from 'es6!display/changes/builds';
import { TimeText } from 'es6!display/time';

import InteractiveData from 'es6!pages/helpers/interactive_data';

import * as api from 'es6!server/api';

var BuildsTab = React.createClass({

  propTypes: {
    // the project api response
    project: PropTypes.object,

    // state is handled by parent so that its preserved if someone selects
    // another tab
    myState: PropTypes.object,

    // The InteractiveData object for this chart
    interactive: PropTypes.object,

    // parent elem that has state
    pageElem: PropTypes.element.isRequired,
  },

  statics: {
    getEndpoint: function(slug) {
      return `/api/0/projects/${slug}/builds/`;
    }
  },

  getInitialState: function() {
    return {};
  },

  componentDidMount: function() {
    if (!this.props.interactive.hasRunInitialize()) {
      var params = this.props.isInitialTab ? InteractiveData.getParamsFromWindowUrl() : null;
      params = params || {};

      this.props.interactive.initialize(params || {});
    }

    // if this table has data to render, let's make sure the window url is
    // correct
    if (api.isLoaded(this.props.interactive.getDataToShow())) {
      this.props.interactive.updateWindowUrl();
    }
  },

  render: function() {
    var interactive = this.props.interactive;

    if (interactive.hasNotLoadedInitialData()) {
      return <APINotLoaded calls={interactive.getDataToShow()} />;
    }

    var data_to_show = interactive.getDataToShow();

    var chart = <ChangesChart
      type="build"
      runnables={data_to_show.getReturnedData()} 
      leftEllipsis={interactive.hasPreviousPage()}
      rightEllipsis={interactive.hasNextPage()}
    />;

    var data = _.map(data_to_show.getReturnedData(), build => {
      var target = null;
      if (_.contains(build.tags, 'arc test')) {
        target = '';
      } else if (build.source.patch) {
        target = <a
          className="external"
          href={build.source.data['phabricator.revisionURL']}
          target="_blank">
          {'D' + build.source.data['phabricator.revisionID']}
        </a>
      } else {
        target = ChangesLinks.phabCommit(build.source.revision);
      }

      return [
        <SingleBuildStatus build={build} parentElem={this} />,
        <a className="subtle" href={ChangesLinks.buildHref(build)}>
          {build.name}
        </a>,
        target,
        get_build_cause(build),
        ChangesLinks.author(build.author),
        <TimeText time={build.dateStarted} />
      ];
    });

    var cellClasses = ['buildWidgetCell', 'wide easyClick', 'nowrap', 'nowrap', 'nowrap', 'nowrap'];

    var headers = [
      'Result',
      'Name',
      'Target',
      'Cause',
      'By',
      'Started'
    ];

    var error_message = null;
    if (interactive.failedToLoadUpdatedData()) {
      error_message = <AjaxError response={interactive.getDataForErrorMessage().response} />;
    }

    var style = interactive.isLoadingUpdatedData() ? {opacity: 0.5} : null;

    return <div>
      <div style={style}>
        <div className="floatR">
          {chart}
        </div>
        {this.renderControls()}
        {error_message}
        <Grid
          colnum={6}
          cellClasses={cellClasses}
          data={data}
          headers={headers}
        />
      </div>
      {this.renderPaging()}
    </div>;
  },

  renderControls: function(commits) {
    var items = [
      'All',
      'Commits Only',
      'Diffs/arc test only'
    ];

    var params_for_items = {
      'All': {
        'include_patches': 1,
        'patches_only': 0
      },
      'Commits Only': {
        'include_patches': 0,
        'patches_only': 0
      },
      'Diffs/arc test only': {
        'include_patches': 1,
        'patches_only': 1
      }
    };

    var current_params = this.props.interactive.getCurrentParams();
    var selected_item = 'All';
    _.each(params_for_items, (params, item) => {
      var is_selected = true;
      _.each(params, (v,k) => {
        if (current_params[k]+"" !== v+"") {
          is_selected = false;
        }
      });
      if (is_selected) {
        selected_item = item;
      }
    });

    var onclick = item => this.props.interactive.updateWithParams(params_for_items[item], true);

    return <Menu1
      className="marginBottomS"
      items={items}
      selectedItem={selected_item}
      onClick={onclick}
    />;
  },

  renderPaging: function(commits) {
    var links = this.props.interactive.getPagingLinks();
    return <div className="marginTopM marginBottomM">{links}</div>;
  },
});

var TODO = React.createClass({
  render: function() {
    return <div>TODO</div>;
  }
});

export default BuildsTab;
